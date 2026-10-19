"""Past, resolved support tickets: the "past tickets" source for RAG.

Tickets created before the March 2026 policy change quote the old 14-day
refund window, so the retrieval layer has stale answers to learn to rank down.
"""

from __future__ import annotations

import random
from datetime import date
from typing import Any

from copilot.seed.common import Dataset, add_days, human_date, iso_dt, messy
from copilot.store import (
    AUTH_HOLD_RELEASE,
    DESCRIPTOR_SUBSCRIPTION,
    POLICY_V2_DATE,
    REFUND_PROCESSING,
    TODAY,
    V1_REFUND_WINDOW_DAYS,
)

PAST_TICKET_COUNT = 300

INTENT_WEIGHTS = {
    "refund_status": 16,
    "refund_request": 18,
    "damaged_or_lost": 12,
    "duplicate_charge": 6,
    "unrecognized_charge": 6,
    "order_change": 7,
    "policy_question": 17,
    "product_question": 13,
    "other": 5,
}

POLICY_QA = [
    ("Free shipping?", "Do you have free shipping? What's the minimum?", "shipping-policy",
     "Standard shipping is free on US orders of $50 or more. Below that it's $6.95."),
    ("Shipping to Canada", "Can you ship to Toronto and how long does it take?", "international-shipping",
     "Yes, we ship to Canada for a flat $19.95, and it takes 7 to 14 business days. Duties are paid on delivery."),
    ("Skip a delivery", "I'm traveling next month, can I skip my subscription delivery?", "subscription-skip-pause",
     "Yes, open Subscriptions in your account and select Skip next delivery at least 1 day before the renewal."),
    ("Price dropped", "The grinder I bought 3 days ago is now cheaper. Can I get the difference?", "price-adjustment",
     "Yes, within 7 days of purchase we refund the price difference once per order."),
    ("Points", "How do the loyalty points work?", "loyalty-program",
     "You earn 1 point per $1 on products, and 100 points are worth $5 off a future order."),
    ("Gift card expiry", "Does a gift card expire?", "gift-cards",
     "No, our gift cards never expire."),
    ("Two codes", "Can I use two promo codes on one order?", "promo-codes",
     "Only one promo code can be used per order."),
    ("Delete my data", "Please delete my account and data.", "privacy-and-data",
     "We have started your deletion request and will confirm within 30 days."),
]  # fmt: skip

PRODUCT_QA = [
    ("Grind for french press", "What grind should I pick for a French press?", "grind-size-guide",
     "Choose Coarse for a French press."),
    ("Decaf", "How is your decaf made? Any chemicals?", "decaf-process",
     "All our decaf uses the Swiss Water Process, with no chemical solvents."),
    ("Sour espresso", "My espresso tastes sour and runs really fast.", "brewing-espresso",
     "That usually means the grind is too coarse: try a finer grind."),
    ("Storage", "Should I keep my beans in the fridge?", "storing-coffee",
     "Please don't refrigerate opened coffee. Keep it sealed somewhere cool and dark."),
    ("Nut allergy", "Is the hazelnut coffee safe with a nut allergy?", "flavored-coffee-allergens",
     "Our flavorings may contain tree nut derivatives, so we would recommend our unflavored coffees."),
    ("Grinder won't start", "My burr grinder won't turn on.", "grinder-troubleshooting",
     "Check that the hopper is locked in place and the bin is fully inserted; the motor won't run otherwise."),
]  # fmt: skip


def _customer_name(ds: Dataset, order: dict[str, Any]) -> str:
    return ds.customer(order["customer_id"])["first_name"]


def _ticket(
    ds: Dataset,
    rng: random.Random,
    *,
    order: dict[str, Any] | None,
    customer: dict[str, Any],
    created: date,
    subject: str,
    body: str,
    reply: str,
    intent: str,
    doc: str | None,
) -> dict[str, Any]:
    opened_at = iso_dt(created, rng)
    replied_at = iso_dt(add_days(created, rng.choice([0, 1])), rng, start_hour=9, end_hour=18)
    if replied_at < opened_at:
        replied_at = opened_at
    return {
        "id": ds.ids.next("ticket"),
        "subject": subject,
        "requester_name": f"{customer['first_name']} {customer['last_name']}",
        "requester_email": customer["email"],
        "created_at": opened_at,
        "status": "solved",
        "channel": rng.choice(["email", "email", "web"]),
        "order_id": order["id"] if order else None,
        "tags": [intent] + ([f"kb:{doc}"] if doc else []),
        "comments": [
            {"author": "customer", "public": True, "created_at": opened_at, "body": body},
            {"author": "agent", "public": True, "created_at": replied_at, "body": reply},
        ],
    }


RETURNABLE_TYPES = ("coffee", "equipment", "merch")


def _day(iso: str) -> date:
    return date.fromisoformat(iso[:10])


def _candidates(ds: Dataset, intent: str) -> list[tuple[dict[str, Any], date]]:
    """Orders that genuinely fit the intent, with the day the customer wrote in."""
    if intent == "refund_status":
        by_order = {o["id"]: o for o in ds.orders}
        return [(by_order[r["order_id"]], add_days(_day(r["created_at"]), 3)) for r in ds.refunds]
    if intent == "duplicate_charge":
        failed = {t["order_id"] for t in ds.transactions if t["kind"] == "sale" and t["status"] == "failure"}
        return [(o, add_days(_day(o["created_at"]), 1)) for o in ds.orders if o["id"] in failed]
    if intent == "unrecognized_charge":
        return [(o, add_days(_day(o["created_at"]), 5)) for o in ds.orders]
    if intent == "order_change":
        return [(o, _day(o["shipped_at"])) for o in ds.orders if o["shipped_at"]]
    # refund_request, damaged_or_lost: delivered orders with a returnable item.
    return [
        (o, add_days(date.fromisoformat(o["delivered_at"]), 4))
        for o in ds.orders
        if o["delivered_at"] and any(li["product_type"] in RETURNABLE_TYPES for li in o["line_items"])
    ]


def _payment_ticket(ds: Dataset, rng: random.Random, intent: str) -> dict[str, Any] | None:
    order, created = rng.choice(_candidates(ds, intent))
    created = add_days(created, rng.randint(0, 4))
    if created >= TODAY:
        return None
    customer = ds.customer(order["customer_id"])
    name = _customer_name(ds, order)
    returnable = [li for li in order["line_items"] if li["product_type"] in RETURNABLE_TYPES]
    item = rng.choice(returnable or order["line_items"])
    old_policy = created < POLICY_V2_DATE

    if intent == "refund_status":
        subject = rng.choice(["Where is my refund?", "Refund not received", f"Refund for {order['name']}"])
        body = rng.choice([
            f"Hi, I returned something from order {order['name']} and haven't seen the refund yet. When will it show up?",
            f"You said my refund for {order['name']} was processed but my card still shows nothing.",
            f"Any update on the refund for {order['name']}? It's been a few days.",
        ])  # fmt: skip
        reply = (
            f"Hi {name}, we issued your refund for {order['name']}. Refunds take {REFUND_PROCESSING} "
            "to appear on your statement, depending on your bank."
        )
        doc = "refund-timelines"
    elif intent == "refund_request":
        subject = rng.choice(["Return request", "I'd like a refund", "Can I return this?"])
        body = rng.choice([
            f"Hello, I'd like to return the {item['title']} from order {order['name']}. It's still sealed.",
            f"I ordered the {item['title']} ({order['name']}) by mistake and haven't opened it. Can I send it back?",
            f"Could I return the {item['title']} from {order['name']}? Never used it.",
        ])  # fmt: skip
        if old_policy:
            reply = (
                f"Hi {name}, no problem. Unopened items can be returned within {V1_REFUND_WINDOW_DAYS} "
                "days of delivery. We'll email you the return details."
            )
        else:
            reply = (
                f"Hi {name}, no problem. Unopened coffee and unused equipment can be returned within "
                "30 days of delivery. We'll email you the return address; return shipping is paid by you."
            )
        doc = "refund-policy-v1" if old_policy else "refund-policy"
    elif intent == "damaged_or_lost":
        subject = rng.choice(["Damaged bag", "Package arrived damaged", "Item broken on arrival"])
        body = rng.choice([
            f"The {item['title']} in order {order['name']} arrived damaged. Photo attached.",
            f"Box for {order['name']} was crushed and the {item['title']} is damaged. Pictures attached.",
        ])  # fmt: skip
        reply = (
            f"Hi {name}, sorry about that. Thanks for the photo: we've sent a replacement for the "
            f"{item['title']}. No need to send the damaged one back."
        )
        doc = "damaged-or-lost-orders"
    elif intent == "duplicate_charge":
        subject = rng.choice(["Charged twice", "Double charge?", "Two charges on my card"])
        body = rng.choice([
            f"I see two charges for order {order['name']} on my card. Why was I charged twice?",
            f"My first payment for {order['name']} failed, I retried, and now there are two charges. Please fix.",
        ])  # fmt: skip
        reply = (
            f"Hi {name}, we only collected payment for {order['name']} once. The second amount is a "
            f"pending authorization from an earlier attempt; your bank releases it within {AUTH_HOLD_RELEASE}."
        )
        doc = "pending-and-duplicate-charges"
    elif intent == "unrecognized_charge":
        subject = rng.choice(["What is this charge?", "Unknown charge on statement", "Didn't order this?"])
        body = f"There's a charge from NWC on my statement for ${order['total']} that I don't recognize."
        if "subscription_renewal" in order["tags"]:
            reply = (
                f"Hi {name}, that charge is your subscription renewal, which appears as "
                f"{DESCRIPTOR_SUBSCRIPTION}. You can skip or cancel future deliveries in your account."
            )
        else:
            reply = (
                f"Hi {name}, that charge is order {order['name']}, placed on "
                f"{human_date(date.fromisoformat(order['created_at'][:10]))} with your card."
            )
        doc = "statement-descriptor"
    else:  # order_change
        subject = rng.choice(["Change my address", "Cancel order", "Wrong grind selected"])
        if rng.random() < 0.5:
            body = f"Can I change order {order['name']}? I picked the wrong grind."
            extra = " You can return unopened coffee within 30 days of delivery."
        else:
            body = f"Please update the shipping address on {order['name']}, I just moved."
            extra = " If it comes back to us as undeliverable, we'll reship it to your new address."
        reply = f"Hi {name}, unfortunately {order['name']} had already shipped, so we can't change it.{extra}"
        doc = "order-changes-and-cancellations"

    if rng.random() < 0.2:
        body = messy(body, rng)
    return _ticket(
        ds, rng, order=order, customer=customer, created=created,
        subject=subject, body=body, reply=reply, intent=intent, doc=doc,
    )  # fmt: skip


def _question_ticket(ds: Dataset, rng: random.Random, intent: str) -> dict[str, Any]:
    customer = rng.choice(ds.customers)
    created = add_days(TODAY, -rng.randint(2, 360))
    if intent == "other":
        subject, body, doc, answer = (
            "Wholesale for our cafe",
            "We run a small cafe and would like to buy your beans wholesale. How does it work?",
            "wholesale",
            "Our wholesale minimum is $500. Please email wholesale@northwind-coffee.example to apply.",
        )
    else:
        bank = POLICY_QA if intent == "policy_question" else PRODUCT_QA
        subject, body, doc, answer = rng.choice(bank)
    if rng.random() < 0.15:
        body = messy(body, rng, shout=False)
    reply = f"Hi {customer['first_name']}, {answer[0].lower()}{answer[1:]}"
    return _ticket(
        ds, rng, order=None, customer=customer, created=created,
        subject=subject, body=body, reply=reply, intent=intent, doc=doc,
    )  # fmt: skip


def build_past_tickets(ds: Dataset, rng: random.Random) -> None:
    intents = list(INTENT_WEIGHTS)
    weights = list(INTENT_WEIGHTS.values())
    while len(ds.tickets) < PAST_TICKET_COUNT:
        intent = rng.choices(intents, weights)[0]
        if intent in ("policy_question", "product_question", "other"):
            ticket = _question_ticket(ds, rng, intent)
        else:
            ticket = _payment_ticket(ds, rng, intent)
        if ticket:
            ds.tickets.append(ticket)
    ds.tickets.sort(key=lambda t: t["created_at"])
