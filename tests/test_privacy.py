"""Personal data redaction: what is removed, what is kept, and that nothing leaks.

The leak scans at the end run the real redaction over all the seed data and the
agent's outputs, and fail if any customer's email, name or card digits survive.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path

import pytest

from copilot import privacy
from copilot.agent import evaluate as agent_eval
from copilot.agent.graph import AgentContext, build_graph
from copilot.agent.prompts import ticket_block
from copilot.agent.rules import Decision, Evidence, guard
from copilot.agent.schemas import Action, Draft
from copilot.agent.scripted import OracleModel, load_eval_cases
from copilot.agent.tools import in_memory_tools
from copilot.kb import load_kb
from copilot.privacy import find_personal_data, redact, redact_text, token
from copilot.rag.retriever import Retriever

DATA = Path(__file__).resolve().parents[1] / "data" / "generated"
CUSTOMERS = [json.loads(line) for line in (DATA / "customers.jsonl").read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Email me at ana.lee@gmail.com please", "ana.lee@gmail.com"),
        ("Call (415) 555-0134 after 5", "555-0134"),
        ("my mobile is +44 20 7946 0958", "7946 0958"),
        ("or 415.555.0199", "555.0199"),
        ("Card 4111 1111 1111 1111 was charged", "4111 1111 1111 1111"),
        ("card 5555555555554444", "5555555555554444"),
        ("refund to my card ending in 9911", "9911"),
        ("the last four digits are 4242", "4242"),
        ("Ship to 12 Harbor Street from now on", "12 Harbor Street"),
        ("logged in from 192.168.1.20", "192.168.1.20"),
        ("Hi Marisol, thanks for writing", "Marisol"),
    ],
)
def test_personal_data_is_removed(text, secret):
    out = redact_text(text)
    assert secret not in out
    assert "[" in out and ":" in out  # replaced with a typed token


@pytest.mark.parametrize(
    "text",
    [
        "Order #1792 was charged $45.35 twice on 2026-10-15.",
        "Refunds take 5 to 10 business days; holds release in 3 to 7.",
        "The Kenya Nyeri 12 oz (SKU NW-101) has a 1-year warranty.",
        "Email wholesale@northwind-coffee.example for cafe accounts.",
        "Tracking 1Z999AA10123456784 and version 2.1.3 at 14:30.",
        "The number 1234567890123 fails the card checksum.",
    ],
)
def test_useful_details_are_kept(text):
    assert redact_text(text) == text


def test_tokens_are_stable_and_keyed(monkeypatch):
    assert token("EMAIL", "a@b.com") == token("EMAIL", "A@B.com ")  # same customer, same token
    before = token("EMAIL", "a@b.com")
    monkeypatch.setenv("COPILOT_REDACTION_KEY", "another-key")
    assert token("EMAIL", "a@b.com") != before  # without the key, tokens cannot be recomputed


def test_structured_data_is_redacted_by_key_and_in_text():
    record = {
        "requester": {"name": "Ana Lee", "email": "ana@x.com"},
        "order": {"name": "#1792", "total": "45.35"},
        "card_last4": "3037",
        "comments": [{"body": "Ana Lee here. Lee is my married name."}],
    }
    out = json.dumps(redact(record))
    for secret in ("Ana", "Lee", "ana@x.com", "3037"):
        assert secret not in out
    assert "#1792" in out and "45.35" in out


def test_the_model_never_sees_contact_or_card_details():
    ticket = {
        "subject": "Refund for Ana",
        "requester": {"name": "Ana Lee", "email": "ana@x.com"},
        "comments": [
            {"customer_text": "I'm Ana Lee, card 4111 1111 1111 1111, call 415-555-0134. Order #1792."}
        ],
    }
    shown = ticket_block(ticket)
    for secret in ("Ana", "Lee", "4111", "555-0134", "ana@x.com"):
        assert secret not in shown
    assert "#1792" in shown


def test_drafts_that_repeat_personal_data_are_flagged():
    draft = Draft(reply="Hi [NAME:1a2b3c4d], we will call 415-555-0134.", summary="x")
    flags = guard(draft, Decision(Action.ANSWER, "x"), Evidence()).flags
    assert any("phone number" in f and "redaction token" in f for f in flags)
    assert find_personal_data("Nothing personal, order #1792 for $45.35.") == []


def test_log_lines_are_redacted():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(privacy.RedactingFilter())
    log = logging.getLogger("test-privacy")
    log.addHandler(handler)
    log.warning("lookup failed for %s", "ana.lee@gmail.com")
    log.removeHandler(handler)
    assert "ana.lee@gmail.com" not in stream.getvalue() and "[EMAIL:" in stream.getvalue()


# Leak scans over real data


def test_no_customer_name_or_email_survives_in_any_ticket():
    tickets = [json.loads(line) for line in (DATA / "tickets.jsonl").read_text(encoding="utf-8").splitlines()]
    tickets += [
        json.loads(line)["ticket"]
        for line in (DATA / "eval_tickets.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    emails = {c["email"] for c in CUSTOMERS}
    names = {c["first_name"] for c in CUSTOMERS} | {c["last_name"] for c in CUSTOMERS}
    leaks = []
    for t in tickets:
        out = json.dumps(redact(t))
        leaks += [(t["id"], e) for e in emails if e in out]
        leaks += [(t["id"], n) for n in names if f'"{n}' in out or f" {n}" in out]
    assert not leaks, leaks[:10]


def test_eval_reports_contain_no_personal_data(tmp_path):
    out = tmp_path / "report.json"
    code = agent_eval.main(["--model", "oracle", "--split", "test", "--out", str(out)])
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert not [c["email"] for c in CUSTOMERS if c["email"] in text]


def test_audit_log_has_no_personal_data():
    """Run real tickets through the agent and scan every audit line the servers wrote."""
    stream = io.StringIO()
    cases = load_eval_cases(DATA / "eval_tickets.jsonl")
    model = OracleModel(cases)
    retriever = Retriever(load_kb(DATA.parents[0] / "kb"))

    async def scenario():
        for case in cases[:12]:
            async with in_memory_tools(agent_eval._servers(retriever, stream)) as tools:
                ctx = AgentContext(tools=tools, triage_model=model, draft_model=model)
                await build_graph().ainvoke({"ticket_id": case["ticket"]["id"]}, context=ctx)

    asyncio.run(scenario())
    log = stream.getvalue()
    assert log.count("\n") > 50  # the agent really called the tools
    assert "get_customer_tickets" in log
    leaks = [c["email"] for c in CUSTOMERS if c["email"] in log]
    leaks += [c["first_name"] for c in CUSTOMERS if f" {c['first_name']} " in log]
    assert not leaks, leaks[:5]


def test_access_log_keeps_its_arguments():
    """uvicorn's access formatter unpacks record.args; redaction must not replace them with None."""
    import logging

    from copilot.privacy import RedactingFilter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.7:51344", "GET", "/", "1.1", 200),
        None,
    )
    assert RedactingFilter().filter(record)
    client, method, path, version, status = record.args
    assert client.startswith("[IP:") and (method, path, status) == ("GET", "/", 200)
    assert "203.0.113.7" not in record.getMessage()
