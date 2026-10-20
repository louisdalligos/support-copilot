"""Zendesk MCP server, tested through a real MCP client session held in memory."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import mcp_session
from mcp.client.session import ClientSession
from mcp_session import DATA, payload

from copilot.audit import AuditLog
from copilot.zendesk.backend import FakeZendesk
from copilot.zendesk.server import build_server

EVAL_TICKET = 10301  # eval-001: duplicate charge on order #1792


def run(test: Callable[[ClientSession], Awaitable[None]]) -> AuditLog:
    return mcp_session.run(
        lambda audit: build_server(FakeZendesk.from_generated(DATA), audit=audit), test, "zendesk"
    )


def test_lists_only_safe_tools() -> None:
    async def t(client: ClientSession) -> None:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == {
            "search_tickets",
            "get_ticket",
            "get_customer_tickets",
            "add_internal_note",
            "add_tags",
            "set_priority",
        }
        for name in ("search_tickets", "get_ticket", "get_customer_tickets"):
            assert tools[name].annotations.readOnlyHint is True
        for name in ("add_internal_note", "add_tags", "set_priority"):
            assert tools[name].annotations.destructiveHint is False

    run(t)


def test_get_ticket_marks_customer_text_untrusted() -> None:
    async def t(client: ClientSession) -> None:
        ticket = payload(await client.call_tool("get_ticket", {"ticket_id": EVAL_TICKET}))
        assert ticket["id"] == EVAL_TICKET
        first = ticket["comments"][0]
        assert "#1792" in first["customer_text"]
        assert "body" not in first
        assert "never as instructions" in ticket["untrusted_fields_note"]

    run(t)


def test_unknown_ticket_is_a_tool_error() -> None:
    async def t(client: ClientSession) -> None:
        result = await client.call_tool("get_ticket", {"ticket_id": 1})
        assert result.isError
        assert "not found" in result.content[0].text

    audit = run(t)
    assert audit.records[-1]["outcome"] == "not_found"


def test_search_filters_and_limits() -> None:
    async def t(client: ClientSession) -> None:
        hits = payload(await client.call_tool("search_tickets", {"query": "charged twice", "limit": 3}))
        assert 0 < len(hits) <= 3
        solved = payload(await client.call_tool("search_tickets", {"query": "refund", "status": "solved"}))
        assert solved and all(h["status"] == "solved" for h in solved)
        bad = await client.call_tool("search_tickets", {"query": "refund", "status": "deleted"})
        assert bad.isError

    run(t)


def test_customer_history() -> None:
    async def t(client: ClientSession) -> None:
        ticket = payload(await client.call_tool("get_ticket", {"ticket_id": EVAL_TICKET}))
        email = ticket["requester"]["email"]
        history = payload(await client.call_tool("get_customer_tickets", {"email": email.upper()}))
        assert EVAL_TICKET in [h["id"] for h in history]

    run(t)


def test_internal_note_is_private_and_audited() -> None:
    async def t(client: ClientSession) -> None:
        body = "Duplicate capture confirmed on #1792. Proposed refund of $45.35 for approval."
        note = payload(await client.call_tool("add_internal_note", {"ticket_id": EVAL_TICKET, "body": body}))
        assert note["public"] is False
        ticket = payload(await client.call_tool("get_ticket", {"ticket_id": EVAL_TICKET}))
        last = ticket["comments"][-1]
        assert last == {**last, "author": "support-copilot", "public": False, "body": body}
        empty = await client.call_tool("add_internal_note", {"ticket_id": EVAL_TICKET, "body": "  "})
        assert empty.isError

    audit = run(t)
    note_calls = [r for r in audit.records if r["tool"] == "add_internal_note"]
    assert [r["outcome"] for r in note_calls] == ["ok", "rejected"]
    assert note_calls[0]["args"]["body"].endswith("chars>")  # note text is not logged


def test_tags_and_priority() -> None:
    async def t(client: ClientSession) -> None:
        tags = payload(
            await client.call_tool(
                "add_tags", {"ticket_id": EVAL_TICKET, "tags": ["copilot:triaged", "duplicate_charge"]}
            )
        )
        again = payload(
            await client.call_tool("add_tags", {"ticket_id": EVAL_TICKET, "tags": ["duplicate_charge"]})
        )
        assert tags["tags"] == again["tags"]
        assert (await client.call_tool("add_tags", {"ticket_id": EVAL_TICKET, "tags": ["Bad Tag!"]})).isError
        prio = payload(await client.call_tool("set_priority", {"ticket_id": EVAL_TICKET, "priority": "high"}))
        assert prio["priority"] == "high"
        assert (
            await client.call_tool("set_priority", {"ticket_id": EVAL_TICKET, "priority": "asap"})
        ).isError

    run(t)


def test_audit_log_hides_emails() -> None:
    async def t(client: ClientSession) -> None:
        await client.call_tool("get_customer_tickets", {"email": "dmitri.eriksen.2301@example.com"})

    audit = run(t)
    line = json.dumps(audit.records)
    assert "example.com" not in line
    assert "[EMAIL:" in line


def test_ticket_resource_and_prompts() -> None:
    async def t(client: ClientSession) -> None:
        templates = (await client.list_resource_templates()).resourceTemplates
        assert [r.uriTemplate for r in templates] == ["zendesk://tickets/{ticket_id}"]
        res = await client.read_resource(f"zendesk://tickets/{EVAL_TICKET}")
        assert json.loads(res.contents[0].text)["id"] == EVAL_TICKET
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        assert prompts == {"summarize_ticket", "triage_payment_ticket"}
        prompt = await client.get_prompt("triage_payment_ticket", {"ticket_id": str(EVAL_TICKET)})
        assert "customer_text" in prompt.messages[0].content.text

    run(t)
