"""Customer risk profiles: signals from refund and dispute history, and how the agent uses them."""

from __future__ import annotations

from datetime import date

from copilot import risk
from copilot.agent.rules import Decision, Evidence, RefundPlan, decide
from copilot.agent.schemas import Action, Intent, Triage

TODAY = date(2026, 10, 15)


def order(total: str, created: str = "2026-01-10T10:00:00Z") -> dict:
    return {"total": total, "created_at": created}


def refund(amount: str, when: str, note: str = "Customer request") -> dict:
    return {"amount": amount, "created_at": when, "note": note}


def test_a_normal_customer_is_low_risk():
    p = risk.profile(
        [order("40.00"), order("30.00")], [refund("10.00", "2026-09-01T10:00:00Z")], [], today=TODAY
    )
    assert p == p | {"level": "low", "reasons": [], "orders": 2, "refunds": 1, "disputes": 0}


def test_recent_refunds():
    two = [refund("5.00", "2026-09-01T10:00:00Z"), refund("5.00", "2026-10-01T10:00:00Z")]
    orders = [order("100.00")] * 4
    assert risk.profile(orders, two, [], today=TODAY)["level"] == "medium"
    three = [*two, refund("5.00", "2026-10-10T10:00:00Z")]
    p = risk.profile(orders, three, [], today=TODAY)
    assert p["level"] == "high" and p["reasons"] == ["3 refunds in the last 90 days"]
    old = [refund("5.00", "2026-03-01T10:00:00Z")] * 3  # outside the 90 days
    assert risk.profile(orders, old, [], today=TODAY)["level"] == "low"


def test_refund_rate_needs_a_pattern():
    one = [refund("40.00", "2026-02-01T10:00:00Z")]
    assert (
        risk.profile([order("40.00")], one, [], today=TODAY)["level"] == "low"
    )  # one refund is not a pattern
    two = [refund("30.00", "2026-02-01T10:00:00Z"), refund("30.00", "2026-03-01T10:00:00Z")]
    p = risk.profile([order("60.00"), order("40.00")], two, [], today=TODAY)
    assert p["level"] == "high" and p["reasons"] == ["$60.00 of $100.00 spent (60%) was refunded"]


def test_duplicate_charge_refunds_do_not_count():
    # Correcting the store's own double capture says nothing about the customer.
    dupes = [refund("20.00", f"2026-10-0{d}T10:00:00Z", note="Duplicate capture") for d in (1, 2, 3)]
    p = risk.profile([order("20.00")] * 3, dupes, [], today=TODAY)
    assert p["level"] == "low" and p["refunds"] == 0


def test_disputes():
    orders = [order("50.00")] * 3
    assert risk.profile(orders, [], [{"status": "won"}], today=TODAY)["level"] == "medium"
    assert risk.profile(orders, [], [{"status": "won"}, {"status": "won"}], today=TODAY)["level"] == "high"
    lost = risk.profile(orders, [], [{"status": "lost"}], today=TODAY)
    assert lost["level"] == "high" and lost["reasons"] == ["1 chargeback the store lost"]
    open_ = risk.profile(orders, [], [{"status": "needs_response"}], today=TODAY)
    assert open_["reasons"] == ["1 chargeback still open"]


def test_new_account_is_only_medium():
    p = risk.profile([order("50.00")], [], [], today=TODAY, customer_since="2026-10-10T09:00:00Z")
    assert p["level"] == "medium" and p["reasons"] == ["account opened 5 days ago"]


def test_strongest_signal_first():
    p = risk.profile(
        [order("50.00")] * 4,
        [refund("5.00", "2026-10-01T10:00:00Z"), refund("5.00", "2026-10-02T10:00:00Z")],
        [{"status": "lost"}],
        today=TODAY,
    )
    assert p["level"] == "high" and p["signals"] == ["lost_dispute", "recent_refunds"]


# How the agent's rules use the profile


def _ev(level: str, eligibility: dict | None = None) -> Evidence:
    return Evidence(
        order={"name": "#1001", "payments": {}, "refund_proposals": []},
        customer={"risk": {"level": level, "reasons": ["3 refunds in the last 90 days"]}},
        line_item={"id": 1, "title": "Kettle"},
        eligibility=eligibility
        or {"outcome": "refund", "reason": "Defective within 30 days of delivery.", "max_amount": "79.00"},
    )


def _triage(intent: Intent = Intent.REFUND_REQUEST) -> Triage:
    return Triage(intent=intent, order_name="#1001", item="Kettle", problem="defective", opened=True)


def test_high_risk_item_refund_is_escalated():
    d = decide(_triage(), _ev("high"), today=TODAY)
    assert d.action is Action.ESCALATE and d.refund is None
    assert "high risk (3 refunds in the last 90 days)" in d.reason


def test_medium_and_low_risk_item_refunds_are_proposed():
    for level in ("low", "medium"):
        assert decide(_triage(), _ev(level), today=TODAY).action is Action.PROPOSE_REFUND


def test_high_risk_duplicate_charge_is_still_refunded():
    ev = _ev("high")
    ev.order = {
        "name": "#1001",
        "payments": {
            "duplicate_capture": True,
            "overcharged": "20.00",
            "refunded": "0.00",
            "successful_charges": 2,
        },
        "refund_proposals": [],
    }
    d = decide(_triage(Intent.DUPLICATE_CHARGE), ev, today=TODAY)
    assert d.action is Action.PROPOSE_REFUND and d.refund == RefundPlan(
        amount="20.00", reason="duplicate_charge"
    )
    assert isinstance(d, Decision)
