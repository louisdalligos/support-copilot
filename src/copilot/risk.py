"""Customer risk profile, from the customer's own history with the store.

The profile says how carefully a person should look before money moves: low,
medium or high, with a plain reason for every signal. The agent's rules use it:
an item refund for a high-risk customer is escalated to a person instead of
proposed automatically (copilot.agent.rules). A refund for a duplicate charge is
not, because that money was taken by the store's mistake.

Only behaviour with this store counts: refunds, disputes and how new the account
is. Nothing about who the customer is (name, location, age or any other personal
trait) is used, and every signal comes with its reason so a person can judge it.

Refunds for duplicate charges are left out: they correct the store's own error
and say nothing about the customer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

LEVELS = ("low", "medium", "high")
RECENT_DAYS = 90  # refunds within this many days count as recent
RECENT_REFUNDS_MEDIUM = 2
RECENT_REFUNDS_HIGH = 3
REFUND_RATE_MEDIUM = Decimal("0.30")  # share of everything spent that was refunded
REFUND_RATE_HIGH = Decimal("0.50")
REFUND_RATE_MIN_REFUNDS = 2  # one refund on one order is not a pattern
NEW_ACCOUNT_DAYS = 14
OPEN_DISPUTE = ("needs_response", "under_review")


@dataclass(frozen=True)
class Signal:
    code: str
    level: str
    reason: str


def _day(value: str | None) -> date | None:
    if not value:
        return None
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        if "T" in value
        else date.fromisoformat(value)
    )


def _store_error(refund: dict[str, Any]) -> bool:
    return "duplicate" in (refund.get("note") or "").lower()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def signals(
    orders: list[dict[str, Any]],
    refunds: list[dict[str, Any]],
    disputes: list[dict[str, Any]],
    *,
    today: date,
    customer_since: str | None = None,
) -> list[Signal]:
    """Every risk signal in a customer's history, strongest first."""
    found: list[Signal] = []
    counted = [r for r in refunds if not _store_error(r)]

    recent = [
        r for r in counted if (d := _day(r.get("created_at"))) and today - d <= timedelta(days=RECENT_DAYS)
    ]
    if len(recent) >= RECENT_REFUNDS_HIGH:
        found.append(
            Signal(
                "recent_refunds", "high", f"{_plural(len(recent), 'refund')} in the last {RECENT_DAYS} days"
            )
        )
    elif len(recent) >= RECENT_REFUNDS_MEDIUM:
        found.append(
            Signal(
                "recent_refunds", "medium", f"{_plural(len(recent), 'refund')} in the last {RECENT_DAYS} days"
            )
        )

    spent = sum((Decimal(o["total"]) for o in orders), Decimal("0"))
    refunded = sum((Decimal(r["amount"]) for r in counted), Decimal("0"))
    if spent > 0 and len(counted) >= REFUND_RATE_MIN_REFUNDS:
        rate = refunded / spent
        share = f"${refunded:.2f} of ${spent:.2f} spent ({rate:.0%}) was refunded"
        if rate >= REFUND_RATE_HIGH:
            found.append(Signal("refund_rate", "high", share))
        elif rate >= REFUND_RATE_MEDIUM:
            found.append(Signal("refund_rate", "medium", share))

    lost = [d for d in disputes if d.get("status") == "lost"]
    open_ = [d for d in disputes if d.get("status") in OPEN_DISPUTE]
    if lost:
        found.append(Signal("lost_dispute", "high", f"{_plural(len(lost), 'chargeback')} the store lost"))
    if open_:
        found.append(Signal("open_dispute", "high", f"{_plural(len(open_), 'chargeback')} still open"))
    if not lost and not open_ and disputes:
        level = "high" if len(disputes) >= 2 else "medium"
        found.append(Signal("disputes", level, f"{_plural(len(disputes), 'chargeback')} filed before"))

    since = _day(customer_since)
    if since and today - since < timedelta(days=NEW_ACCOUNT_DAYS):
        found.append(Signal("new_account", "medium", f"account opened {(today - since).days} days ago"))

    return sorted(found, key=lambda s: -LEVELS.index(s.level))


def profile(
    orders: list[dict[str, Any]],
    refunds: list[dict[str, Any]],
    disputes: list[dict[str, Any]],
    *,
    today: date,
    customer_since: str | None = None,
) -> dict[str, Any]:
    """The customer's risk level (the strongest signal) and the reasons for it."""
    found = signals(orders, refunds, disputes, today=today, customer_since=customer_since)
    counted = [r for r in refunds if not _store_error(r)]
    return {
        "level": found[0].level if found else "low",
        "reasons": [s.reason for s in found],
        "signals": [s.code for s in found],
        "orders": len(orders),
        "refunds": len(counted),
        "disputes": len(disputes),
    }
