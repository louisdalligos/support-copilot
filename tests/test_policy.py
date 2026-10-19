from datetime import timedelta
from decimal import Decimal

import pytest

from copilot.policy import Outcome, Problem, check_refund, refundable_balance
from copilot.store import TODAY


def delivered(days_ago):
    return TODAY - timedelta(days=days_ago)


@pytest.mark.parametrize(
    ("item_type", "days", "opened", "problem", "final_sale", "expected"),
    [
        ("coffee", 10, False, Problem.NONE, False, Outcome.REFUND),
        ("coffee", 30, False, Problem.NONE, False, Outcome.REFUND),  # last day of the window
        ("coffee", 31, False, Problem.NONE, False, Outcome.DECLINE),
        ("coffee", 5, True, Problem.NONE, False, Outcome.DECLINE),  # opened, change of mind
        ("coffee", 14, True, Problem.DAMAGED, False, Outcome.REFUND),
        ("coffee", 15, True, Problem.DAMAGED, False, Outcome.DECLINE),
        ("coffee", 20, True, Problem.DEFECTIVE, False, Outcome.REFUND),  # stale coffee within 30 days
        ("coffee", 40, True, Problem.DEFECTIVE, False, Outcome.DECLINE),
        ("equipment", 12, True, Problem.DEFECTIVE, False, Outcome.REFUND),
        ("equipment", 120, True, Problem.DEFECTIVE, False, Outcome.WARRANTY),
        ("equipment", 400, True, Problem.DEFECTIVE, False, Outcome.DECLINE),
        ("merch", 6, False, Problem.NONE, True, Outcome.DECLINE),  # final sale
        ("merch", 6, False, Problem.DAMAGED, True, Outcome.REFUND),  # final sale still covers damage
        ("gift_card", 1, False, Problem.NONE, False, Outcome.DECLINE),
    ],
)
def test_refund_rules(item_type, days, opened, problem, final_sale, expected):
    decision = check_refund(
        item_type=item_type,
        delivered_on=delivered(days),
        opened=opened,
        problem=problem,
        final_sale=final_sale,
    )
    assert decision.outcome is expected


def test_unshipped_orders_can_be_cancelled():
    decision = check_refund(
        item_type="coffee", delivered_on=None, opened=False, problem=Problem.NONE, shipped=False
    )
    assert decision.outcome is Outcome.REFUND
    assert decision.policy_doc == "order-changes-and-cancellations"


def test_unshipped_subscription_renewal_is_refundable():
    decision = check_refund(
        item_type="subscription", delivered_on=None, opened=False, problem=Problem.NONE, shipped=False
    )
    assert decision.outcome is Outcome.REFUND
    assert decision.policy_doc == "subscription-terms"


def test_in_transit_orders_are_not_refunded_yet():
    decision = check_refund(item_type="coffee", delivered_on=None, opened=False, problem=Problem.NONE)
    assert decision.outcome is Outcome.DECLINE


def test_refundable_balance_never_negative():
    assert refundable_balance(Decimal("20.00"), Decimal("5.00")) == Decimal("15.00")
    assert refundable_balance(Decimal("20.00"), Decimal("25.00")) == Decimal("0.00")
