"""Open chargebacks with known right answers, for the dispute-evidence feature.

Each case builds a customer, their orders and, where it matters, a past support
conversation, then opens a dispute that needs a response. Its label says what a
careful merchant would do (respond with evidence, or accept) and which evidence
must be in the packet. The cases draw from their own random stream, so adding
them leaves every other generated row as it was.
"""

from __future__ import annotations

import random
from typing import Any

from copilot.seed.common import Dataset, add_days, iso_dt, money
from copilot.seed.evals import Builder
from copilot.seed.orders import add_dispute, add_refund
from copilot.store import TODAY


def _past_ticket(
    ds: Dataset, rng: random.Random, customer: dict[str, Any], order: dict[str, Any], days_ago: int,
    subject: str, customer_text: str, agent_text: str, tags: list[str],
) -> None:  # fmt: skip
    asked = iso_dt(add_days(TODAY, -days_ago), rng, end_hour=14)
    answered = asked[:11] + "17:05:00Z"
    ds.tickets.append(
        {
            "id": ds.ids.next("ticket"),
            "subject": subject,
            "requester_name": f"{customer['first_name']} {customer['last_name']}",
            "requester_email": customer["email"],
            "created_at": asked,
            "status": "solved",
            "channel": "email",
            "order_id": order["id"],
            "tags": tags,
            "comments": [
                {"author": "customer", "public": True, "created_at": asked, "body": customer_text},
                {"author": "agent", "public": True, "created_at": answered, "body": agent_text},
            ],
        }
    )


def build_dispute_cases(ds: Dataset) -> None:
    rng = random.Random("northwind-dispute-cases")
    b = Builder(ds, rng)
    blend = b.product("coffee", contains="Dark Harbor 12 oz")
    kettle = b.product("equipment", contains="Gooseneck Kettle 1L")
    grinder = b.product("equipment", contains="Northwind Burr Grinder")
    house = b.product("coffee", contains="House Blend 2 lb")

    def case(group: str, dispute: dict[str, Any], recommendation: str, evidence: list[str], why: str) -> None:
        ds.dispute_cases.append(
            {
                "group": group,
                "dispute_id": dispute["id"],
                "order_id": dispute["order_id"],
                "reason": dispute["reason"],
                "expected_recommendation": recommendation,
                "expected_evidence": evidence,
                "why": why,
            }
        )

    # 1. "Not received", but the carrier delivered it and the customer wrote to us about it afterwards.
    c = b.customer()
    o = b.order(c, [(kettle, 1), (blend, 1)], created_days_ago=26, delivered_days_ago=21)
    _past_ticket(
        ds, rng, c, o, 18, "Kettle temperature",
        f"Hi, my kettle from {o['name']} arrived on Tuesday. What temperature should I use for a light roast pour-over?",
        "Hi! For light roasts we suggest 96 to 98 C (205 to 208 F). Enjoy the new kettle.",
        ["product_question"],
    )  # fmt: skip
    d = add_dispute(
        ds, rng, o, reason="product_not_received", status="needs_response", day=add_days(TODAY, -4)
    )
    case("dispute-delivered-and-confirmed", d, "respond",
         ["shipping_documentation", "customer_communication"],
         "The carrier delivered the order, and the customer wrote to support about the item after delivery.")  # fmt: skip

    # 2. "Fraudulent", from a long-standing customer whose earlier orders went to the same place undisputed.
    c = b.customer()
    for days in (300, 210, 140, 70):
        b.order(c, [(house, 1)], created_days_ago=days, delivered_days_ago=days - 4)
    o = b.order(c, [(grinder, 1)], created_days_ago=15, delivered_days_ago=11)
    d = add_dispute(ds, rng, o, reason="fraudulent", status="needs_response", day=add_days(TODAY, -6))
    case("dispute-fraud-returning-customer", d, "respond",
         ["shipping_documentation", "prior_undisputed_orders"],
         "Four earlier orders from the same account and address over ten months, none disputed.")  # fmt: skip

    # 3. "Product unacceptable", but the store already refunded it in full before the dispute.
    c = b.customer()
    o = b.order(c, [(blend, 2)], created_days_ago=20, delivered_days_ago=16)
    add_refund(ds, rng, o, money(o["total"]), day=add_days(TODAY, -12), note="Damaged bag")
    _past_ticket(
        ds, rng, c, o, 13, "Bags arrived damaged",
        f"Both bags in {o['name']} arrived torn. Can I get a refund?",
        f"Sorry about that! We've refunded the full ${o['total']} to your card; it takes 5 to 10 business days to appear.",
        ["damaged_or_lost"],
    )  # fmt: skip
    d = add_dispute(
        ds, rng, o, reason="product_unacceptable", status="needs_response", day=add_days(TODAY, -9)
    )
    case("dispute-already-refunded", d, "respond",
         ["refund_record", "customer_communication"],
         "The full amount was refunded before the dispute was opened (a credit-issued response).")  # fmt: skip

    # 4. "Not received", and the parcel really never arrived: no proof of delivery, so accept.
    c = b.customer()
    o = b.order(c, [(house, 1)], created_days_ago=30, shipped=True)
    d = add_dispute(
        ds, rng, o, reason="product_not_received", status="needs_response", day=add_days(TODAY, -12)
    )
    case("dispute-never-delivered", d, "accept", [],
         "Shipped but never marked delivered: there is no proof of delivery to send.")  # fmt: skip
