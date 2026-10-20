"""Refund checks shared by the Shopify MCP server and the approval app.

The same check runs twice: when the agent files a proposal (through the MCP
server), and again when a person approves it, against the order as it is at
that moment. Between the two, a refund may have been issued elsewhere or a
second proposal approved; the second check catches that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from copilot.policy import Problem, check_refund, refundable_balance
from copilot.shopify.backend import ShopifyBackend

CENT = Decimal("0.01")
REASONS = ("duplicate_charge", "item_refund")
PROBLEMS = ("none", "damaged", "wrong_item", "defective")


class RefundError(ValueError):
    """A refund or proposal that the store's rules do not allow. The message says why."""


def money(value: Decimal) -> str:
    return str(value.quantize(CENT))


def parse_amount(text: str) -> Decimal:
    try:
        amount = Decimal(str(text).strip().lstrip("$"))
    except InvalidOperation:
        raise RefundError(f"amount must be a number like 12.50, got {text!r}") from None
    if not amount.is_finite() or amount <= 0:
        raise RefundError("amount must be greater than zero")
    if amount != amount.quantize(CENT):
        raise RefundError("amount must have at most two decimal places")
    return amount


def payment_summary(
    order: dict[str, Any], txs: list[dict[str, Any]], refunds: list[dict[str, Any]]
) -> dict[str, Any]:
    """What was collected, refunded and still refundable, and whether the order was captured twice."""
    captures = [t for t in txs if t["kind"] == "sale" and t["status"] == "success"]
    failed = [t for t in txs if t["kind"] == "sale" and t["status"] == "failure"]
    captured = sum((Decimal(t["amount"]) for t in captures), Decimal("0"))
    refunded = sum((Decimal(r["amount"]) for r in refunds), Decimal("0"))
    total = Decimal(order["total"])
    overcharge = max(Decimal("0"), captured - total)
    return {
        "order_total": money(total),
        "captured": money(captured),
        "successful_charges": len(captures),
        "failed_attempts": len(failed),
        "failed_attempts_note": "Failed attempts collect no money; any hold they leave is released by the bank."
        if failed
        else None,
        "duplicate_capture": len(captures) > 1 and overcharge > 0,
        "overcharged": money(overcharge),
        "refunded": money(refunded),
        "refundable_balance": money(refundable_balance(captured, refunded)),
    }


def order_detail(backend: ShopifyBackend, name: str) -> dict[str, Any]:
    order = backend.order(name)
    txs = backend.transactions(order["id"])
    refunds = backend.refunds(order["id"])
    return {
        **order,
        "payments": payment_summary(order, txs, refunds),
        "transactions": txs,
        "refunds": refunds,
        "disputes": backend.disputes(order["id"]),
        "refund_proposals": backend.proposals(order["id"]),
    }


def line_item(order: dict[str, Any], line_item_id: int) -> dict[str, Any]:
    for item in order["line_items"]:
        if item["id"] == line_item_id:
            return item
    ids = ", ".join(str(i["id"]) for i in order["line_items"])
    raise RefundError(f"Order {order['name']} has no line item {line_item_id}; its items are {ids}")


def eligibility(
    order: dict[str, Any], item: dict[str, Any], opened: bool, problem: Problem
) -> dict[str, Any]:
    delivered = order.get("delivered_at")
    decision = check_refund(
        item_type=item["product_type"],
        delivered_on=date.fromisoformat(delivered) if delivered else None,
        opened=opened,
        problem=problem,
        final_sale=item.get("final_sale", False),
        shipped=order.get("shipped_at") is not None,
    )
    item_total = Decimal(item["price"]) * item["quantity"]
    return {
        "order": order["name"],
        "line_item_id": item["id"],
        "title": item["title"],
        "outcome": decision.outcome.value,
        "refundable": decision.refundable,
        "reason": decision.reason,
        "policy_doc": decision.policy_doc,
        "max_amount": money(item_total) if decision.refundable else None,
    }


@dataclass
class CheckedRefund:
    order: dict[str, Any]
    amount: Decimal
    policy: dict[str, Any] | None


def check_refund_request(
    backend: ShopifyBackend,
    *,
    order_name: str,
    amount: str,
    reason: str,
    line_item_id: int | None = None,
    opened: bool | None = None,
    problem: str | None = None,
    exclude_proposal_id: int | None = None,
) -> CheckedRefund:
    """Raise RefundError unless this refund is allowed on the order as it is now.

    Pending proposals count against the balance, except `exclude_proposal_id`:
    when approving a proposal, it must not block itself.
    """
    if reason not in REASONS:
        raise RefundError(f"reason must be one of {', '.join(REASONS)}")
    value = parse_amount(amount)
    detail = order_detail(backend, order_name)
    pay = detail["payments"]
    pending = sum(
        (
            Decimal(p["amount"])
            for p in detail["refund_proposals"]
            if p["status"] == "pending_approval" and p["id"] != exclude_proposal_id
        ),
        Decimal("0"),
    )
    available = Decimal(pay["refundable_balance"]) - pending
    if value > available:
        raise RefundError(
            f"amount {money(value)} is more than the {money(max(available, Decimal('0')))} "
            "still refundable on this order (after refunds and pending proposals)"
        )

    policy = None
    if reason == "duplicate_charge":
        if not pay["duplicate_capture"]:
            raise RefundError(f"Order {detail['name']} was not captured more than once")
        # Refunds already issued or proposed count against the excess, so the
        # duplicate cannot be refunded twice.
        excess_left = Decimal(pay["overcharged"]) - Decimal(pay["refunded"]) - pending
        if value > excess_left:
            raise RefundError(
                f"amount is more than the {money(max(excess_left, Decimal('0')))} charged in excess "
                "that is not already refunded or in a pending proposal"
            )
    else:
        if line_item_id is None or opened is None or problem is None:
            raise RefundError("item_refund needs line_item_id, opened and problem")
        if problem not in PROBLEMS:
            raise RefundError(f"problem must be one of {', '.join(PROBLEMS)}")
        item = line_item(detail, line_item_id)
        policy = eligibility(detail, item, opened, Problem(problem))
        if not policy["refundable"]:
            raise RefundError(f"Policy does not allow this refund: {policy['reason']}")
        if value > Decimal(policy["max_amount"]):
            raise RefundError(f"amount is more than the item's price, {policy['max_amount']}")
    return CheckedRefund(order=detail, amount=value, policy=policy)
