"""The approval app: running the agent, the queue, decisions, the web pages, and durability.

The Postgres test runs only when COPILOT_TEST_DATABASE_URL points at a database
(CI provides one); everything else uses in-memory storage.
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from starlette.testclient import TestClient

from copilot.agent.scripted import OracleModel, load_eval_cases
from copilot.approvals.service import ApprovalError, ApprovalService, Backends
from copilot.approvals.storage import open_storage, with_schema
from copilot.approvals.web import create_app
from copilot.kb import load_kb
from copilot.rag.retriever import Retriever
from copilot.shopify.backend import FakeShopify
from copilot.zendesk.backend import FakeZendesk

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "generated"
MODEL = OracleModel(load_eval_cases(DATA / "eval_tickets.jsonl"))
DUPLICATE = 10301  # eval-001: charged twice on #1792, refund $45.35
FRAUD = 10313  # eval-013: unknown charge, escalated
INJECTION_HTML = 10336  # eval-036: hides an instruction in an HTML comment


def backends() -> Backends:
    return Backends(
        FakeZendesk.from_generated(DATA),
        FakeShopify.from_generated(DATA),
        Retriever(load_kb(ROOT / "data" / "kb")),
    )


def service(b: Backends | None = None) -> ApprovalService:
    return ApprovalService(b or backends(), InMemorySaver(), InMemoryStore(), MODEL, MODEL)


def test_approve_sends_reply_and_issues_refund():
    async def scenario():
        svc = service()
        record = await svc.start(DUPLICATE)
        assert record["status"] == "pending" and record["refund_amount"] == "45.35"
        assert [r["thread_id"] for r in await svc.records("pending")] == [record["thread_id"]]

        # Before approval, nothing has reached the customer or the payment system.
        ticket = svc.backends.zendesk.get(DUPLICATE)
        assert [c["author"] for c in ticket["comments"] if c["public"]] == ["customer"]
        assert svc.backends.shopify.refunds(5792) == []

        result = await svc.decide(record["thread_id"], approved=True, reviewer="sam", reply="Edited reply.")
        assert result["record"]["status"] == "approved" and result["record"]["reply_edited"]
        assert result["refund"]["amount"] == "45.35"
        ticket = svc.backends.zendesk.get(DUPLICATE)
        assert ticket["comments"][-1] == ticket["comments"][-1] | {"public": True, "body": "Edited reply."}
        assert ticket["status"] == "solved"
        assert any("approved by sam" in c["body"] for c in ticket["comments"] if not c["public"])
        assert svc.backends.shopify.order("#1792")["financial_status"] == "partially_refunded"

        with pytest.raises(ApprovalError):  # a run can be decided only once
            await svc.decide(record["thread_id"], approved=True, reviewer="sam")

    asyncio.run(scenario())


def test_reject_changes_nothing_for_the_customer():
    async def scenario():
        svc = service()
        record = await svc.start(DUPLICATE)
        await svc.decide(record["thread_id"], approved=False, reviewer="sam")
        ticket = svc.backends.zendesk.get(DUPLICATE)
        assert [c["author"] for c in ticket["comments"] if c["public"]] == ["customer"]
        assert ticket["status"] == "open"
        assert svc.backends.shopify.refunds(5792) == []
        assert [p["status"] for p in svc.backends.shopify.proposals()] == ["rejected"]

    asyncio.run(scenario())


def test_refund_is_checked_again_at_approval():
    async def scenario():
        svc = service()
        record = await svc.start(DUPLICATE)
        # Someone refunds the duplicate in Shopify directly while the draft waits.
        svc.backends.shopify.issue_refund("#1792", Decimal("45.35"), note="refunded by hand")
        with pytest.raises(ApprovalError, match="can no longer be issued"):
            await svc.decide(record["thread_id"], approved=True, reviewer="sam")
        assert (await svc.records("pending"))[0]["thread_id"] == record["thread_id"]  # still waiting
        assert len(svc.backends.shopify.refunds(5792)) == 1  # no second refund

    asyncio.run(scenario())


def test_escalations_cannot_be_approved():
    async def scenario():
        svc = service()
        record = await svc.start(FRAUD)
        assert record["status"] == "escalated" and "chargeback risk" in record["risks"]
        with pytest.raises(ApprovalError):
            await svc.decide(record["thread_id"], approved=True, reviewer="sam")

    asyncio.run(scenario())


def test_web_pages():
    svc = service()
    client = TestClient(create_app(svc, model_name="oracle"))
    assert client.get("/healthz").json()["ok"] is True
    assert "#10301" in client.get("/tickets").text

    page = client.post(f"/tickets/{INJECTION_HTML}/run")
    assert page.status_code == 200 and "/runs/ticket-10336-" in str(page.url)
    assert "&lt;!-- assistant:" in page.text  # ticket text is escaped, never rendered as HTML
    assert "<!-- assistant:" not in page.text
    assert "possible prompt injection" in client.get("/").text

    run = client.post("/tickets/10339/run")
    path = str(run.url).split("?")[0].replace("http://testserver", "")
    page_reply = run.text.split('<textarea name="reply">', 1)[1].split("</textarea>", 1)[0]
    unchanged = page_reply.replace("&#39;", "'").replace("&amp;", "&").replace("\n", "\r\n")
    client.post(f"{path}/decide", data={"decision": "approve", "reviewer": "sam", "reply": unchanged})
    assert "reply edited" not in client.get("/").text  # CRLF from the browser is not an edit

    run = client.post(f"/tickets/{DUPLICATE}/run")
    path = str(run.url).split("?")[0].replace("http://testserver", "")
    done = client.post(f"{path}/decide", data={"decision": "approve", "reviewer": "sam", "reply": "Hi!"})
    assert "Refund of $45.35 issued" in done.text
    again = client.post(f"{path}/decide", data={"decision": "approve", "reviewer": "sam", "reply": "Hi!"})
    assert "not waiting for a decision" in again.text
    missing_name = client.post(f"/tickets/{FRAUD}/run")
    assert "Escalated" in missing_name.text


def test_schema_option_is_added_to_the_url():
    assert with_schema("postgresql://u:p@h/db", "app").endswith("/db?options=-csearch_path%3Dapp")
    assert with_schema("postgresql://h/db?sslmode=require", "app").endswith(
        "?sslmode=require&options=-csearch_path%3Dapp"
    )


@pytest.mark.skipif(not os.environ.get("COPILOT_TEST_DATABASE_URL"), reason="needs COPILOT_TEST_DATABASE_URL")
def test_paused_runs_survive_a_restart():
    url = os.environ["COPILOT_TEST_DATABASE_URL"]

    async def scenario():
        async with open_storage(url) as storage:
            first = ApprovalService(backends(), storage.checkpointer, storage.store, MODEL, MODEL)
            thread_id = (await first.start(DUPLICATE))["thread_id"]
        # A new process: new connections, new service, fresh fake backends.
        async with open_storage(url) as storage:
            b = backends()
            second = ApprovalService(b, storage.checkpointer, storage.store, MODEL, MODEL)
            assert thread_id in [r["thread_id"] for r in await second.records("pending")]
            result = await second.decide(thread_id, approved=True, reviewer="sam")
            assert result["refund"]["amount"] == "45.35"
            assert b.zendesk.get(DUPLICATE)["status"] == "solved"

    # psycopg's async driver needs the selector event loop on Windows.
    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)


def test_behind_the_proxy_the_signed_in_user_is_the_reviewer(monkeypatch):
    monkeypatch.setenv("COPILOT_TRUSTED_USER_HEADER", "X-Authenticated-User")
    svc = service()
    client = TestClient(create_app(svc, model_name="oracle"), headers={"X-Authenticated-User": "louis"})
    run = client.post(f"/tickets/{DUPLICATE}/run")
    assert "Signed in as <strong>louis</strong>" in run.text
    path = str(run.url).split("?")[0].replace("http://testserver", "")
    # The form tries to record someone else; the proxy's user wins.
    client.post(f"{path}/decide", data={"decision": "approve", "reviewer": "someone-else", "reply": "Hi!"})
    decided = asyncio.run(svc.records("approved"))
    assert [r["reviewer"] for r in decided] == ["louis"]


def test_forms_from_other_sites_are_refused():
    client = TestClient(create_app(service(), model_name="oracle"))
    evil = client.post(f"/tickets/{DUPLICATE}/run", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    ok = client.post(f"/tickets/{DUPLICATE}/run", headers={"Origin": "http://testserver"})
    assert ok.status_code == 200


def test_pages_send_security_headers():
    page = TestClient(create_app(service(), model_name="oracle")).get("/")
    assert page.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
