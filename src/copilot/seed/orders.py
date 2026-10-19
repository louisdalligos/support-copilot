"""Orders and their payment records: transactions, refunds and disputes.

The builders here are used twice: to generate the background order history,
and by the eval scenarios to build orders with exact, known properties.
"""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal
from typing import Any

from copilot.seed.common import Dataset, add_days, fmt_money, iso_dt, money
from copilot.store import (
    DESCRIPTOR_ORDER,
    DESCRIPTOR_SUBSCRIPTION,
    EXPRESS_SHIPPING,
    FREE_SHIPPING_THRESHOLD,
    INTERNATIONAL_SHIPPING,
    POLICY_V2_DATE,
    STANDARD_SHIPPING,
    TODAY,
    V1_FREE_SHIPPING_THRESHOLD,
    V1_STANDARD_SHIPPING,
)

HISTORY_START = date(2025, 10, 1)


def shipping_cost(
    subtotal: Decimal, created: date, *, country: str, express: bool, subscription: bool
) -> Decimal:
    if country != "US":
        return INTERNATIONAL_SHIPPING
    if subscription:
        return Decimal("0.00")
    if express:
        return EXPRESS_SHIPPING
    v2 = created >= POLICY_V2_DATE
    threshold = FREE_SHIPPING_THRESHOLD if v2 else V1_FREE_SHIPPING_THRESHOLD
    if subtotal >= threshold:
        return Decimal("0.00")
    return STANDARD_SHIPPING if v2 else V1_STANDARD_SHIPPING


def new_order(
    ds: Dataset,
    rng: random.Random,
    customer: dict[str, Any],
    items: list[tuple[dict[str, Any], int]],
    *,
    created: date,
    shipped: bool = True,
    transit_days: int | None = None,
    express: bool = False,
    capture: bool = True,
) -> dict[str, Any]:
    """Create an order with one successful payment capture.

    transit_days: days from shipping to delivery. None with shipped=True means
    the package has not been delivered yet (in transit or lost).
    """
    order_id = ds.ids.next("order")
    subscription = any(p["product_type"] == "subscription" for p, _ in items)
    line_items = []
    subtotal = Decimal("0.00")
    for product, qty in items:
        price = money(product["price"])
        subtotal += price * qty
        line_items.append(
            {
                "id": ds.ids.next("line_item"),
                "product_id": product["id"],
                "title": product["title"],
                "product_type": product["product_type"],
                "quantity": qty,
                "price": fmt_money(price),
                "final_sale": product["final_sale"],
            }
        )
    shipping = shipping_cost(
        subtotal, created, country=customer["country"], express=express, subscription=subscription
    )
    total = subtotal + shipping

    shipped_on = add_days(created, 1) if shipped else None
    delivered_on = add_days(shipped_on, transit_days) if shipped_on and transit_days is not None else None
    if delivered_on and delivered_on > TODAY:
        delivered_on = None

    order = {
        "id": order_id,
        "name": f"#{order_id - 4000}",
        "customer_id": customer["id"],
        "email": customer["email"],
        "created_at": iso_dt(created, rng),
        "currency": "USD",
        "line_items": line_items,
        "subtotal": fmt_money(subtotal),
        "shipping": fmt_money(shipping),
        "total": fmt_money(total),
        "financial_status": "paid",
        "fulfillment_status": "fulfilled" if shipped else "unfulfilled",
        "shipped_at": shipped_on.isoformat() if shipped_on else None,
        "delivered_at": delivered_on.isoformat() if delivered_on else None,
        "shipping_country": customer["country"],
        "tags": ["subscription_renewal"] if subscription else [],
    }
    ds.orders.append(order)
    if capture:
        add_transaction(ds, rng, order, kind="sale", status="success", amount=total, day=created)
    return order


def descriptor_for(order: dict[str, Any]) -> str:
    return DESCRIPTOR_SUBSCRIPTION if "subscription_renewal" in order["tags"] else DESCRIPTOR_ORDER


def add_transaction(
    ds: Dataset,
    rng: random.Random,
    order: dict[str, Any],
    *,
    kind: str,
    status: str,
    amount: Decimal,
    day: date,
) -> dict[str, Any]:
    txn = {
        "id": ds.ids.next("transaction"),
        "order_id": order["id"],
        "kind": kind,
        "status": status,
        "amount": fmt_money(amount),
        "created_at": iso_dt(day, rng),
        "gateway": "shopify_payments",
        "descriptor": descriptor_for(order),
        "card_last4": str(1000 + (order["customer_id"] * 37) % 9000),
    }
    ds.transactions.append(txn)
    return txn


def paid_amount(ds: Dataset, order: dict[str, Any]) -> Decimal:
    return sum(
        (
            money(t["amount"])
            for t in ds.transactions
            if t["order_id"] == order["id"] and t["kind"] == "sale" and t["status"] == "success"
        ),
        Decimal("0.00"),
    )


def refunded_amount(ds: Dataset, order: dict[str, Any]) -> Decimal:
    return sum((money(r["amount"]) for r in ds.refunds if r["order_id"] == order["id"]), Decimal("0.00"))


def add_refund(
    ds: Dataset, rng: random.Random, order: dict[str, Any], amount: Decimal, *, day: date, note: str
) -> dict[str, Any]:
    paid = paid_amount(ds, order)
    already = refunded_amount(ds, order)
    if already + amount > paid:
        raise ValueError(f"refund of {amount} would exceed {paid - already} left on {order['name']}")
    txn = add_transaction(ds, rng, order, kind="refund", status="success", amount=amount, day=day)
    refund = {
        "id": ds.ids.next("refund"),
        "order_id": order["id"],
        "amount": fmt_money(amount),
        "created_at": txn["created_at"],
        "note": note,
        "transaction_id": txn["id"],
    }
    ds.refunds.append(refund)
    order["financial_status"] = "refunded" if already + amount >= paid else "partially_refunded"
    return refund


def add_dispute(
    ds: Dataset, rng: random.Random, order: dict[str, Any], *, reason: str, status: str, day: date
) -> dict[str, Any]:
    dispute = {
        "id": ds.ids.next("dispute"),
        "order_id": order["id"],
        "amount": order["total"],
        "reason": reason,
        "status": status,
        "initiated_at": iso_dt(day, rng),
        "evidence_due_by": add_days(day, 14).isoformat(),
    }
    ds.disputes.append(dispute)
    return dispute


def _pick_items(ds: Dataset, rng: random.Random, customer: dict[str, Any]) -> list[tuple[dict, int]]:
    coffee = [p for p in ds.products if p["product_type"] == "coffee"]
    other = [p for p in ds.products if p["product_type"] in ("equipment", "merch", "gift_card")]
    if customer["country"] == "US" and rng.random() < 0.18:
        subs = [p for p in ds.products if p["product_type"] == "subscription"]
        return [(rng.choice(subs), 1)]
    items = [(rng.choice(coffee), rng.choice([1, 1, 1, 2]))]
    if rng.random() < 0.35:
        items.append((rng.choice(other), 1))
    if rng.random() < 0.2:
        items.append((rng.choice(coffee), 1))
    return items


def build_order_history(ds: Dataset, rng: random.Random) -> None:
    """Background orders: about a year of normal activity, with a little noise."""
    span = (TODAY - HISTORY_START).days
    for customer in list(ds.customers):
        for _ in range(rng.choice([0, 1, 1, 2, 2, 3, 3, 4, 5, 6])):
            created = add_days(HISTORY_START, int(span * rng.random() ** 0.7))
            if created >= TODAY:
                created = add_days(TODAY, -1)
            age = (TODAY - created).days
            shipped = age >= 1
            transit = rng.randint(7, 14) if customer["country"] != "US" else rng.randint(2, 5)
            order = new_order(
                ds,
                rng,
                customer,
                _pick_items(ds, rng, customer),
                created=created,
                shipped=shipped,
                transit_days=transit,
                express=customer["country"] == "US" and rng.random() < 0.08,
                capture=False,
            )
            total = money(order["total"])
            roll = rng.random()
            if roll < 0.03:
                # A declined first attempt: the bank may still show a pending hold.
                add_transaction(ds, rng, order, kind="sale", status="failure", amount=total, day=created)
            add_transaction(ds, rng, order, kind="sale", status="success", amount=total, day=created)
            if roll > 0.995:
                # A rare genuine double capture.
                add_transaction(ds, rng, order, kind="sale", status="success", amount=total, day=created)

            if order["delivered_at"] and rng.random() < 0.06:
                delivered = date.fromisoformat(order["delivered_at"])
                line = rng.choice(order["line_items"])
                amount = money(line["price"]) if rng.random() < 0.7 else total
                when = add_days(delivered, rng.randint(2, 12))
                if when < TODAY:
                    add_refund(ds, rng, order, amount, day=when, note="Customer request")
            if order["delivered_at"] and rng.random() < 0.006:
                delivered = date.fromisoformat(order["delivered_at"])
                when = add_days(delivered, rng.randint(10, 40))
                if when < TODAY:
                    status = "under_review" if (TODAY - when).days < 30 else rng.choice(["won", "lost"])
                    reason = rng.choice(["fraudulent", "unrecognized", "product_not_received"])
                    add_dispute(ds, rng, order, reason=reason, status=status, day=when)
