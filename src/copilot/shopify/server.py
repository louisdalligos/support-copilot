"""Shopify MCP server.

Gives the agent read access to orders, payments and customers, a refund
eligibility check, and one write: filing a refund proposal for a person to
approve. There is no tool that issues a refund, cancels an order or changes
a customer.

Every proposal is checked in code before it is filed: the amount must fit
what is still refundable, a duplicate-charge refund needs a real duplicate
capture, and an item refund must pass the refund policy rules in
copilot.policy. A model can ask for anything; only valid proposals get through.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from copilot.audit import AuditLog
from copilot.policy import Problem
from copilot.shopify.backend import FakeShopify, NotFound, ShopifyBackend
from copilot.shopify.refunds import (
    RefundError,
    check_refund_request,
    eligibility,
    line_item,
    money,
    order_detail,
    parse_amount,
)

MAX_LIMIT = 50

INSTRUCTIONS = """\
Orders, payments and customers for Northwind Coffee Co.
Use get_order for an order's items, payments, refunds and disputes.
Use check_refund_eligibility before proposing an item refund.
propose_refund only files a proposal; a person approves or rejects it. You cannot issue refunds."""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
PROPOSE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)


def build_server(backend: ShopifyBackend, *, audit: AuditLog | None = None, **settings: Any) -> FastMCP:
    audit = audit or AuditLog("shopify")
    mcp = FastMCP("shopify", instructions=INSTRUCTIONS, **settings)

    def audited(tool: str, args: dict[str, Any], fn: Callable[[], Any]) -> Any:
        try:
            result = fn()
        except NotFound as e:
            audit.record(tool, args, "not_found")
            raise ToolError(str(e)) from None
        except (ToolError, RefundError) as e:
            audit.record(tool, args, "rejected", str(e))
            raise ToolError(str(e)) from None
        audit.record(tool, args, "ok")
        return result

    @mcp.tool(title="Get order", annotations=READ)
    def get_order(order_name: str) -> dict[str, Any]:
        """Get an order by its number (like #1792): items, shipping and delivery dates, every payment
        attempt, refunds, disputes, and a payment summary that flags duplicate captures."""
        return audited("get_order", {"order_name": order_name}, lambda: order_detail(backend, order_name))

    @mcp.tool(title="Get customer", annotations=READ)
    def get_customer(email: str, order_limit: int = 10) -> dict[str, Any]:
        """Get a customer by email, with their most recent orders and how many disputes they have filed."""

        def run() -> dict[str, Any]:
            customer = backend.customer(email)
            orders = backend.orders_for(email, limit=min(max(order_limit, 1), MAX_LIMIT))
            disputes = sum(len(backend.disputes(o["id"])) for o in backend.orders_for(email, limit=1000))
            return {
                **customer,
                "dispute_count": disputes,
                "recent_orders": [
                    {
                        "name": o["name"],
                        "created_at": o["created_at"],
                        "total": o["total"],
                        "financial_status": o["financial_status"],
                        "fulfillment_status": o["fulfillment_status"],
                        "items": [i["title"] for i in o["line_items"]],
                    }
                    for o in orders
                ],
            }

        return audited("get_customer", {"email": email, "order_limit": order_limit}, run)

    @mcp.tool(title="Find charges by amount", annotations=READ)
    def find_charges(amount: str, limit: int = 10) -> list[dict[str, Any]]:
        """Find card charges of an exact amount, to identify a charge a customer does not recognize.

        Returns each charge's order, date, statement descriptor, card last 4 digits and status."""

        def run() -> list[dict[str, Any]]:
            hits = backend.transactions_matching(parse_amount(amount), limit=min(max(limit, 1), MAX_LIMIT))
            keep = ("order_name", "amount", "created_at", "descriptor", "card_last4", "status")
            return [{k: t[k] for k in keep} for t in hits]

        return audited("find_charges", {"amount": amount, "limit": limit}, run)

    @mcp.tool(title="Check refund eligibility", annotations=READ)
    def check_refund_eligibility(
        order_name: str,
        line_item_id: int,
        opened: bool,
        problem: Literal["none", "damaged", "wrong_item", "defective"],
    ) -> dict[str, Any]:
        """Check one item against the refund policy. Set problem to none for a change of mind.

        Returns refund, decline or warranty, the reason, and the policy document to cite."""
        args = {"order_name": order_name, "line_item_id": line_item_id, "opened": opened, "problem": problem}

        def run() -> dict[str, Any]:
            order = backend.order(order_name)
            return eligibility(order, line_item(order, line_item_id), opened, Problem(problem))

        return audited("check_refund_eligibility", args, run)

    @mcp.tool(title="Propose refund", annotations=PROPOSE)
    def propose_refund(
        order_name: str,
        amount: str,
        reason: Literal["duplicate_charge", "item_refund"],
        ticket_id: int,
        explanation: str,
        line_item_id: int | None = None,
        opened: bool | None = None,
        problem: Literal["none", "damaged", "wrong_item", "defective"] | None = None,
    ) -> dict[str, Any]:
        """File a refund proposal for a person to approve. It does NOT refund anything.

        For reason=duplicate_charge, the order must have been captured more than once.
        For reason=item_refund, give line_item_id, opened and problem; the item must pass the policy check.
        The amount can never exceed what is still refundable on the order."""
        args = {
            "order_name": order_name,
            "amount": amount,
            "reason": reason,
            "ticket_id": ticket_id,
            "line_item_id": line_item_id,
            "opened": opened,
            "problem": problem,
        }

        def run() -> dict[str, Any]:
            checked = check_refund_request(
                backend,
                order_name=order_name,
                amount=amount,
                reason=reason,
                line_item_id=line_item_id,
                opened=opened,
                problem=problem,
            )
            detail, value, policy = checked.order, checked.amount, checked.policy
            filed = backend.file_proposal(
                {
                    "order_id": detail["id"],
                    "order_name": detail["name"],
                    "amount": money(value),
                    "reason": reason,
                    "ticket_id": ticket_id,
                    "line_item_id": line_item_id,
                    "explanation": explanation.strip(),
                    "policy_reason": policy["reason"] if policy else None,
                    "policy_doc": policy["policy_doc"] if policy else "pending-and-duplicate-charges",
                }
            )
            return {**filed, "note": "Nothing has been refunded. A person must approve this proposal."}

        return audited("propose_refund", args, run)

    @mcp.resource("shopify://orders/{order_number}", mime_type="application/json")
    def order_resource(order_number: str) -> dict[str, Any]:
        """One order with payments, refunds and disputes. Use the number without '#', like 1792."""
        try:
            return order_detail(backend, order_number)
        except NotFound as e:
            raise ValueError(str(e)) from None

    @mcp.prompt(title="Investigate a charge")
    def investigate_charge(order_name: str) -> str:
        """Ask for an explanation of the charges on an order."""
        return (
            f"Call get_order for {order_name}. Explain each charge on it in plain language: which were "
            "collected, which failed, whether the order was captured twice, and what has been refunded. "
            "If it was captured twice, propose a refund of the excess with propose_refund."
        )

    return mcp


def default_data_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "generated"


def build_default_server(data_dir: Path | None = None, **settings: Any) -> FastMCP:
    return build_server(FakeShopify.from_generated(data_dir or default_data_dir()), **settings)
