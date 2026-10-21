"""Shopify MCP server, tested through a real MCP client session held in memory.

The refund tests run every labeled eval ticket through the server: the
eligibility check and the proposal tool must agree with the eval labels.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import mcp_session
from mcp.client.session import ClientSession
from mcp_session import DATA, error_text, payload

from copilot.audit import AuditLog
from copilot.shopify.backend import FakeShopify
from copilot.shopify.server import build_server

EVALS = [json.loads(line) for line in (DATA / "eval_tickets.jsonl").read_text(encoding="utf-8").splitlines()]
ITEM_CASES = [e for e in EVALS if e["labels"]["refund_inputs"] and e["labels"]["order_name"]]


def run(test: Callable[[ClientSession], Awaitable[None]]) -> AuditLog:
    return mcp_session.run(
        lambda audit: build_server(FakeShopify.from_generated(DATA), audit=audit), test, "shopify"
    )


def mentioned_item(order: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """The line item the ticket is about: the one whose title it mentions, or the only one."""
    text = case["ticket"]["comments"][0]["body"].lower()
    named = [i for i in order["line_items"] if i["title"].lower() in text]
    items = named or order["line_items"]
    assert len(items) == 1, (case["case_id"], [i["title"] for i in order["line_items"]])
    return items[0]


def test_tools_cannot_issue_refunds() -> None:
    async def t(client: ClientSession) -> None:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == {
            "get_order",
            "get_customer",
            "find_charges",
            "check_refund_eligibility",
            "propose_refund",
        }
        writes = [n for n, tool in tools.items() if not tool.annotations.readOnlyHint]
        assert writes == ["propose_refund"]

    run(t)


def test_duplicate_capture_is_flagged() -> None:
    async def t(client: ClientSession) -> None:
        dup = payload(await client.call_tool("get_order", {"order_name": "#1792"}))["payments"]
        assert dup["duplicate_capture"] is True
        assert dup["overcharged"] == "45.35"
        retry = payload(await client.call_tool("get_order", {"order_name": "1793"}))["payments"]
        assert retry["duplicate_capture"] is False
        assert retry["failed_attempts"] >= 1
        assert "collect no money" in retry["failed_attempts_note"]

    run(t)


def test_unknown_order() -> None:
    async def t(client: ClientSession) -> None:
        assert "not found" in error_text(await client.call_tool("get_order", {"order_name": "#99999"}))

    run(t)


def test_eligibility_matches_eval_labels() -> None:
    assert len(ITEM_CASES) >= 10

    async def t(client: ClientSession) -> None:
        for case in ITEM_CASES:
            labels = case["labels"]
            order = payload(await client.call_tool("get_order", {"order_name": labels["order_name"]}))
            item = mentioned_item(order, case)
            result = payload(
                await client.call_tool(
                    "check_refund_eligibility",
                    {
                        "order_name": labels["order_name"],
                        "line_item_id": item["id"],
                        **labels["refund_inputs"],
                    },
                )
            )
            assert result["reason"] == labels["policy_reason"], case["case_id"]
            # A high-risk customer's refund passes the policy but is escalated (copilot.risk).
            policy_allows = labels["expected_action"] == "propose_refund" or labels["customer_risk"] == "high"
            assert result["refundable"] == policy_allows, case["case_id"]

    run(t)


def test_proposals_follow_eval_labels() -> None:
    # A fresh server per case: two phrasings of one scenario share an order, and the
    # second proposal on an order is (correctly) limited by the first one.
    for case in ITEM_CASES:
        run(_propose_like_label(case))


def _propose_like_label(case: dict[str, Any]) -> Callable[[ClientSession], Awaitable[None]]:
    async def t(client: ClientSession) -> None:
        labels = case["labels"]
        order = payload(await client.call_tool("get_order", {"order_name": labels["order_name"]}))
        item = mentioned_item(order, case)
        args = {
            "order_name": labels["order_name"],
            "amount": labels["refund_amount"] or item["price"],
            "reason": "item_refund",
            "ticket_id": case["ticket"]["id"],
            "explanation": "test",
            "line_item_id": item["id"],
            **labels["refund_inputs"],
        }
        result = await client.call_tool("propose_refund", args)
        if labels["expected_action"] == "propose_refund":
            filed = payload(result)
            assert filed["status"] == "pending_approval"
            assert filed["amount"] == labels["refund_amount"]
        elif labels["customer_risk"] == "high":
            assert "High-risk customer" in error_text(result), case["case_id"]
        else:
            assert "Policy does not allow" in error_text(result), case["case_id"]

    return t


def test_duplicate_charge_proposal_is_checked() -> None:
    base = {"reason": "duplicate_charge", "ticket_id": 10301, "explanation": "Captured twice."}

    async def t(client: ClientSession) -> None:
        too_much = await client.call_tool(
            "propose_refund", {**base, "order_name": "#1792", "amount": "90.70"}
        )
        assert "excess" in error_text(too_much) or "refundable" in error_text(too_much)
        not_dup = await client.call_tool("propose_refund", {**base, "order_name": "#1793", "amount": "10.00"})
        assert "not captured more than once" in error_text(not_dup)

        filed = payload(
            await client.call_tool("propose_refund", {**base, "order_name": "#1792", "amount": "$45.35"})
        )
        assert filed["amount"] == "45.35"
        assert "Nothing has been refunded" in filed["note"]
        again = await client.call_tool("propose_refund", {**base, "order_name": "#1792", "amount": "45.35"})
        assert "pending proposal" in error_text(again)  # a second proposal cannot double the refund

        order = payload(await client.call_tool("get_order", {"order_name": "#1792"}))
        assert order["financial_status"] == "paid"  # proposing changed nothing on the order
        assert [p["status"] for p in order["refund_proposals"]] == ["pending_approval"]

    run(t)


def test_bad_amounts_are_rejected() -> None:
    base = {"order_name": "#1792", "reason": "duplicate_charge", "ticket_id": 10301, "explanation": "x"}

    async def t(client: ClientSession) -> None:
        for amount in ("-5", "0", "abc", "1.005", "NaN"):
            assert error_text(await client.call_tool("propose_refund", {**base, "amount": amount}))

    run(t)


def test_find_charges_and_customer() -> None:
    async def t(client: ClientSession) -> None:
        charges = payload(await client.call_tool("find_charges", {"amount": "14.40"}))
        assert charges and all(c["amount"] == "14.40" for c in charges)
        assert any(c["descriptor"] == "NWC*COFFEE SUBSCRIPTION" for c in charges)
        assert payload(await client.call_tool("find_charges", {"amount": "83.17"})) == []

        customer = payload(await client.call_tool("get_customer", {"email": "TARA.HADDAD.2001@example.com"}))
        assert customer["id"] == 2001
        assert customer["recent_orders"]

    audit = run(t)
    assert "example.com" not in json.dumps(audit.records)


def test_order_resource() -> None:
    async def t(client: ClientSession) -> None:
        res = await client.read_resource("shopify://orders/1792")
        assert json.loads(res.contents[0].text)["name"] == "#1792"

    run(t)


def test_customer_risk_profile() -> None:
    risky = next(c for c in EVALS if c["group"] == "risk-lost-chargeback")
    normal = next(c for c in EVALS if c["group"] == "dup-true")

    async def t(client: ClientSession) -> None:
        got = payload(await client.call_tool("get_customer", {"email": risky["ticket"]["requester_email"]}))
        assert got["risk"]["level"] == "high" and got["risk"]["reasons"] == ["1 chargeback the store lost"]
        assert got["dispute_count"] == 1
        ok = payload(await client.call_tool("get_customer", {"email": normal["ticket"]["requester_email"]}))
        assert ok["risk"]["level"] == "low" and ok["risk"]["reasons"] == []

    run(t)
