"""Refund rules from the store's refund policy, written as code.

The agent never decides on its own whether a refund is allowed: every refund
proposal is checked here first. The seed generator uses the same function to
label eval tickets, so the labels and the runtime check cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from copilot.store import (
    DAMAGE_REPORT_DAYS,
    DEFECTIVE_COFFEE_DAYS,
    REFUND_WINDOW_DAYS,
    TODAY,
    WARRANTY_DAYS,
)


class Problem(StrEnum):
    NONE = "none"  # change of mind
    DAMAGED = "damaged"
    WRONG_ITEM = "wrong_item"
    DEFECTIVE = "defective"


class Outcome(StrEnum):
    REFUND = "refund"
    DECLINE = "decline"
    WARRANTY = "warranty"  # repair or replace under warranty, not a refund


@dataclass(frozen=True)
class RefundDecision:
    outcome: Outcome
    reason: str
    policy_doc: str

    @property
    def refundable(self) -> bool:
        return self.outcome is Outcome.REFUND


def check_refund(
    *,
    item_type: str,
    delivered_on: date | None,
    opened: bool,
    problem: Problem,
    final_sale: bool = False,
    shipped: bool = True,
    today: date = TODAY,
) -> RefundDecision:
    """Decide whether one item may be refunded under the current refund policy.

    item_type is one of: coffee, equipment, subscription, gift_card, merch.
    """
    if item_type == "gift_card":
        return RefundDecision(Outcome.DECLINE, "Gift cards cannot be refunded.", "gift-cards")

    if item_type == "subscription" and not shipped:
        return RefundDecision(
            Outcome.REFUND, "Subscription renewal has not shipped yet.", "subscription-terms"
        )

    if not shipped:
        return RefundDecision(
            Outcome.REFUND,
            "Order has not shipped yet, so it can be cancelled in full.",
            "order-changes-and-cancellations",
        )

    if delivered_on is None:
        return RefundDecision(
            Outcome.DECLINE,
            "Order is still in transit; refunds are handled after delivery or as a lost parcel.",
            "damaged-or-lost-orders",
        )

    days = (today - delivered_on).days

    if problem in (Problem.DAMAGED, Problem.WRONG_ITEM):
        if days <= DAMAGE_REPORT_DAYS:
            return RefundDecision(
                Outcome.REFUND,
                f"Damaged or wrong item reported {days} days after delivery (limit {DAMAGE_REPORT_DAYS}).",
                "refund-policy",
            )
        return RefundDecision(
            Outcome.DECLINE,
            f"Damaged or wrong item reported {days} days after delivery (limit {DAMAGE_REPORT_DAYS}).",
            "refund-policy",
        )

    if problem is Problem.DEFECTIVE:
        if item_type == "equipment":
            if days <= REFUND_WINDOW_DAYS:
                return RefundDecision(
                    Outcome.REFUND,
                    f"Defective equipment within {REFUND_WINDOW_DAYS} days of delivery.",
                    "refund-policy",
                )
            if days <= WARRANTY_DAYS:
                return RefundDecision(
                    Outcome.WARRANTY,
                    f"Defective equipment {days} days after delivery is covered by the warranty.",
                    "equipment-warranty",
                )
            return RefundDecision(
                Outcome.DECLINE, "Equipment is outside the 1-year warranty.", "equipment-warranty"
            )
        if days <= DEFECTIVE_COFFEE_DAYS:
            return RefundDecision(
                Outcome.REFUND,
                f"Defective coffee reported within {DEFECTIVE_COFFEE_DAYS} days of delivery.",
                "refund-policy",
            )
        return RefundDecision(
            Outcome.DECLINE,
            f"Defective coffee reported {days} days after delivery (limit {DEFECTIVE_COFFEE_DAYS}).",
            "refund-policy",
        )

    # Change of mind.
    if final_sale:
        return RefundDecision(Outcome.DECLINE, "Final sale items cannot be refunded.", "refund-policy")
    if opened:
        return RefundDecision(
            Outcome.DECLINE,
            "Opened coffee or used equipment cannot be returned for a change of mind.",
            "refund-policy",
        )
    if days <= REFUND_WINDOW_DAYS:
        return RefundDecision(
            Outcome.REFUND,
            f"Unopened item returned {days} days after delivery (limit {REFUND_WINDOW_DAYS}).",
            "refund-policy",
        )
    return RefundDecision(
        Outcome.DECLINE,
        f"Return requested {days} days after delivery (limit {REFUND_WINDOW_DAYS}).",
        "refund-policy",
    )


def refundable_balance(paid: Decimal, already_refunded: Decimal) -> Decimal:
    """The most that can still be refunded on an order. Never negative."""
    return max(Decimal("0.00"), paid - already_refunded)
