"""Chargeback evidence: what the store sends the card issuer to contest a dispute.

The packet follows how Shopify Payments and Stripe take evidence, and what they
advise: a short, neutral rebuttal tied to the dispute's reason, a timeline in date
order, and only the evidence that answers that reason (delivery proof for "not
received", earlier undisputed orders for "fraudulent", the refund for a refund
claim). The fields map one to one onto Shopify's ShopifyPaymentsDisputeEvidenceUpdateInput,
and everything is also rendered as one PDF (copilot.disputes.pdf).

Everything here is assembled by code from the store's records. There is no model in
this path: what goes to a bank must be exactly what the records say. A person reviews
the packet and submits it; nothing is sent from here.

When the evidence cannot win (no proof of delivery for a "not received" claim, say),
the recommendation is to accept the dispute, as the payment providers advise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from copilot.kb import KbDoc, load_kb
from copilot.shopify.backend import ShopifyBackend
from copilot.shopify.refunds import order_detail
from copilot.store import STORE_NAME
from copilot.store import today as store_today
from copilot.zendesk.backend import ZendeskBackend

KB_DIR = Path(__file__).resolve().parents[3] / "data" / "kb"
OPEN = ("needs_response", "under_review")
REASONS = {
    "product_not_received": "Product not received",
    "fraudulent": "Fraudulent",
    "unrecognized": "Unrecognized charge",
    "product_unacceptable": "Product unacceptable",
    "credit_not_processed": "Credit not processed",
    "subscription_canceled": "Subscription canceled",
    "duplicate": "Duplicate charge",
    "general": "General",
}
FRAUD = ("fraudulent", "unrecognized")
REFUND_CLAIMS = ("product_unacceptable", "credit_not_processed", "subscription_canceled", "general")
PRIOR_ORDERS_NEEDED = 2
EXCERPT_CHARS = 600


class DisputeNotFound(LookupError):
    pass


def _day(stamp: str | None) -> date | None:
    if not stamp:
        return None
    return (
        datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()
        if "T" in stamp
        else date.fromisoformat(stamp)
    )


def _long(day: date | None) -> str:
    return f"{day.strftime('%B')} {day.day}, {day.year}" if day else "unknown"


def policy_section(doc_id: str, heading: str, kb_dir: Path = KB_DIR) -> dict[str, str] | None:
    """One section of a policy, as the customer saw it: the excerpt, not the whole document."""
    docs: list[KbDoc] = [d for d in load_kb(kb_dir) if d.id == doc_id and d.is_current]
    if not docs:
        return None
    doc = docs[0]
    parts = re.split(r"^## +(.+)$", doc.body, flags=re.MULTILINE)
    for name, text in zip(parts[1::2], parts[2::2], strict=False):
        if name.strip() == heading:
            return {"title": f"{doc.title}: {heading}", "url": doc.url, "text": " ".join(text.split())}
    return None


@dataclass
class Facts:
    dispute: dict[str, Any]
    order: dict[str, Any]
    customer: dict[str, Any]
    other_orders: list[dict[str, Any]]
    messages: list[dict[str, Any]]


def _facts(shopify: ShopifyBackend, zendesk: ZendeskBackend | None, dispute_id: int) -> Facts:
    found = [d for d in shopify.list_disputes() if d["id"] == dispute_id]
    if not found:
        raise DisputeNotFound(f"No chargeback {dispute_id}")
    dispute = found[0]
    order = order_detail(shopify, dispute["order_name"])
    email = order["email"]
    try:
        customer = shopify.customer(email)
    except LookupError:
        customer = {"email": email}
    others = [o for o in shopify.orders_for(email, limit=1000) if o["id"] != order["id"]]
    prior = []
    for o in others:
        if o["created_at"] < order["created_at"] and not shopify.disputes(o["id"]):
            prior.append(o)
    messages = []
    if zendesk is not None:
        for t in zendesk.by_requester(email, limit=20):
            text = " ".join(c["body"] for c in t["comments"])
            if order["name"] in text or t["created_at"] >= order["created_at"]:
                messages.append(t)
    return Facts(dispute, order, customer, sorted(prior, key=lambda o: o["created_at"]), messages)


def _excerpts(ticket: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for c in ticket["comments"]:
        if not c.get("public", True):
            continue  # internal notes are the store's own, not communication with the customer
        body = " ".join(c["body"].split())
        out.append(
            {
                "date": c["created_at"][:10],
                "from": "Customer" if c["author"] == "customer" else STORE_NAME,
                "text": body if len(body) <= EXCERPT_CHARS else body[: EXCERPT_CHARS - 1] + "…",
            }
        )
    return out


def build_packet(
    shopify: ShopifyBackend, zendesk: ZendeskBackend | None, dispute_id: int, *, today: date | None = None
) -> dict[str, Any]:
    """The evidence packet for one chargeback: recommendation, rebuttal, timeline, evidence, Shopify fields."""
    today = today or store_today()
    f = _facts(shopify, zendesk, dispute_id)
    d, o, c = f.dispute, f.order, f.customer
    reason = d["reason"]
    name = " ".join(p for p in (c.get("first_name"), c.get("last_name")) if p) or "The customer"
    placed, shipped, delivered = _day(o["created_at"]), _day(o.get("shipped_at")), _day(o.get("delivered_at"))
    opened, due = _day(d["initiated_at"]), _day(d["evidence_due_by"])
    region = ", ".join(p for p in (c.get("province"), o.get("shipping_country")) if p)
    items = ", ".join(f"{i['quantity']} x {i['title']}" for i in o["line_items"])
    sale = next((t for t in o["transactions"] if t["kind"] == "sale" and t["status"] == "success"), {})
    refunds = o["refunds"]
    refunded = sum((Decimal(r["amount"]) for r in refunds), Decimal("0"))
    refunded_before = [r for r in refunds if r["created_at"] < d["initiated_at"]]
    amount = Decimal(d["amount"])

    evidence: dict[str, dict[str, Any]] = {}
    if delivered:
        evidence["shipping_documentation"] = {
            "title": "Shipping and delivery",
            "summary": f"Shipped on {_long(shipped)} and marked delivered by the carrier on {_long(delivered)}"
            + (f", to {region}." if region else "."),
            "rows": [
                ["Order placed", _long(placed)],
                ["Shipped", _long(shipped)],
                ["Delivered", _long(delivered)],
            ]
            + ([["Ship to", region]] if region else []),
        }
    after = [
        t for t in f.messages if delivered and _day(t["created_at"]) and _day(t["created_at"]) >= delivered
    ]
    talk = after if reason == "product_not_received" else f.messages
    if talk:
        evidence["customer_communication"] = {
            "title": "Communication with the customer",
            "summary": f"{len(talk)} support conversation{'s' if len(talk) > 1 else ''} with the customer"
            + (", written after the delivery date." if reason == "product_not_received" else "."),
            "messages": [m for t in talk for m in _excerpts(t)],
        }
    if reason in FRAUD and len(f.other_orders) >= PRIOR_ORDERS_NEEDED:
        evidence["prior_undisputed_orders"] = {
            "title": "Earlier orders from the same account, none disputed",
            "summary": f"{len(f.other_orders)} earlier orders from {o['email']}"
            + (f", shipped to {region}" if region else "")
            + ", delivered and never disputed.",
            "rows": [[p["name"], _long(_day(p["created_at"])), f"${p['total']}"] for p in f.other_orders],
        }
    if refunds:
        evidence["refund_record"] = {
            "title": "Refunds already issued",
            "summary": f"${refunded:.2f} refunded to the original card"
            + (" before the dispute was opened." if refunded_before else "."),
            "rows": [[_long(_day(r["created_at"])), f"${r['amount']}", r.get("note") or ""] for r in refunds],
        }
    policy = None
    if reason in REFUND_CLAIMS:
        policy = policy_section("refund-policy", "Damaged, defective or wrong items")

    # What a careful merchant would do, and why.
    recommendation, why = "respond", ""
    if reason == "product_not_received":
        if delivered:
            why = "The carrier marked the order delivered" + (
                ", and the customer wrote to support about it afterwards."
                if "customer_communication" in evidence
                else "."
            )
        else:
            recommendation = "accept"
            why = "The order was never marked delivered, so there is no proof of delivery to send. Accept the dispute (or refund the order) rather than contest it."
    elif reason in FRAUD:
        if "prior_undisputed_orders" in evidence and delivered:
            why = "The account has a history of undisputed orders to the same place, and this one was delivered."
        elif delivered:
            why = "The order was delivered, but there is no earlier history with this account. Fraud claims are hard to win without proof the cardholder made the purchase; respond only if you have more."
        else:
            recommendation = "accept"
            why = "No delivery and no account history to show the cardholder made the purchase."
    elif refunded_before and sum((Decimal(r["amount"]) for r in refunded_before), Decimal("0")) >= amount:
        why = "The disputed amount was refunded in full before the dispute was opened."
    elif reason in REFUND_CLAIMS and delivered:
        why = "The order was delivered; send the refund policy and any conversation with the customer."
    else:
        recommendation = "accept"
        why = "The records do not contradict the claim."

    # Strongest evidence for this reason first: the issuer reads from the top.
    if refunded_before:
        first = ["refund_record", "customer_communication"]
    elif reason in FRAUD:
        first = ["prior_undisputed_orders", "shipping_documentation"]
    else:
        first = ["shipping_documentation", "customer_communication"]
    evidence = {k: evidence[k] for k in [*first, *evidence] if k in evidence}

    rebuttal = _rebuttal(
        reason, recommendation, name, o, items, placed, shipped, delivered, region, f, refunded_before, opened
    )
    timeline = sorted(
        [{"date": placed.isoformat(), "event": f"Order {o['name']} placed, ${o['total']} ({items})"}]
        + ([{"date": shipped.isoformat(), "event": "Shipped"}] if shipped else [])
        + (
            [{"date": delivered.isoformat(), "event": "Delivered (carrier confirmation)"}]
            if delivered
            else []
        )
        + [
            {"date": m["date"], "event": f"{m['from']} wrote to support"}
            for m in evidence.get("customer_communication", {}).get("messages", [])
        ]
        + [{"date": r["created_at"][:10], "event": f"Refund of ${r['amount']} issued"} for r in refunds]
        + [
            {
                "date": opened.isoformat(),
                "event": f"Chargeback opened: {REASONS.get(reason, reason)}, ${d['amount']}",
            }
        ],
        key=lambda e: e["date"],
    )
    return {
        "dispute": {
            "id": d["id"],
            "reason": reason,
            "reason_label": REASONS.get(reason, reason.replace("_", " ").capitalize()),
            "amount": d["amount"],
            "status": d["status"],
            "opened": opened.isoformat(),
            "due": due.isoformat() if due else None,
            "days_left": (due - today).days if due else None,
            "open": d["status"] in OPEN,
        },
        "order": {
            "name": o["name"],
            "placed": placed.isoformat(),
            "items": [
                {"title": i["title"], "quantity": i["quantity"], "price": i["price"]} for i in o["line_items"]
            ],
            "total": o["total"],
            "card_last4": sale.get("card_last4"),
            "descriptor": sale.get("descriptor"),
            "shipped": shipped.isoformat() if shipped else None,
            "delivered": delivered.isoformat() if delivered else None,
            "ship_to": region,
        },
        "customer": {"name": name, "email": o["email"]},
        "recommendation": {"action": recommendation, "why": why},
        "rebuttal": rebuttal,
        "timeline": timeline,
        "evidence": evidence,
        "policy": policy,
        "shopify_fields": _shopify_fields(c, o, reason, rebuttal, policy, evidence),
    }


def _rebuttal(
    reason: str, recommendation: str, name: str, o: dict[str, Any], items: str, placed: date | None,
    shipped: date | None, delivered: date | None, region: str, f: Facts, refunded_before: list[dict[str, Any]],
    opened: date | None,
) -> str:  # fmt: skip
    if recommendation == "accept":
        return ""
    where = f" in {region}" if region else ""
    first = f"{name} ordered {items} on {_long(placed)} (order {o['name']}, ${o['total']})."
    if refunded_before:
        total = sum((Decimal(r["amount"]) for r in refunded_before), Decimal("0"))
        when = _long(_day(refunded_before[-1]["created_at"]))
        return (
            f"{first} On {when}, before this dispute was opened on {_long(opened)}, we refunded ${total:.2f} "
            "to the original card (refund record attached). The cardholder has already received the disputed amount."
        )
    if reason == "product_not_received":
        text = (
            f"{first} We shipped it on {_long(shipped)} to the address on the order{where}, and the carrier "
            f"marked it delivered on {_long(delivered)} (delivery record attached)."
        )
        comm = [
            m
            for t in f.messages
            for m in _excerpts(t)
            if m["from"] == "Customer" and delivered and m["date"] >= delivered.isoformat()
        ]
        if comm:
            text += f" On {_long(_day(comm[0]['date']))} the customer wrote to our support team about the order after it arrived (attached)."
        return text + " The claim that the order was not received is not supported by these records."
    if reason in FRAUD:
        text = f"{first}"
        if len(f.other_orders) >= PRIOR_ORDERS_NEEDED:
            since = _long(_day(f.other_orders[0]["created_at"]))
            text += (
                f" It was placed from the customer's own account ({o['email']}), which has placed "
                f"{len(f.other_orders)} earlier orders since {since}, shipped to the same region{where}; none were disputed."
            )
        if delivered:
            text += f" This order was delivered on {_long(delivered)}."
        return text + " We ask that the dispute be reversed."
    text = f"{first}"
    if delivered:
        text += f" It was delivered on {_long(delivered)}."
    return text + " Our refund policy (excerpt attached) and our conversation with the customer are included."


def _shopify_fields(
    c: dict[str, Any], o: dict[str, Any], reason: str, rebuttal: str, policy: dict[str, str] | None,
    evidence: dict[str, Any],
) -> dict[str, Any]:  # fmt: skip
    """Shopify's ShopifyPaymentsDisputeEvidenceUpdateInput, filled from the packet. The PDF goes in
    uncategorizedFile; submitEvidence stays false: a person submits."""
    fields: dict[str, Any] = {
        "customerEmailAddress": o["email"],
        "customerFirstName": c.get("first_name"),
        "customerLastName": c.get("last_name"),
        "uncategorizedText": rebuttal,
        "uncategorizedFile": "evidence.pdf",
        "submitEvidence": False,
    }
    if c.get("province") or o.get("shipping_country"):
        fields["shippingAddress"] = {
            "provinceCode": c.get("province"),
            "countryCode": o.get("shipping_country"),
        }
    if policy:
        fields["refundPolicyDisclosure"] = policy["text"]
    if "refund_record" in evidence:
        fields["refundRefusalExplanation"] = evidence["refund_record"]["summary"]
    return {k: v for k, v in fields.items() if v not in (None, "")}


def open_disputes(shopify: ShopifyBackend, *, today: date | None = None) -> list[dict[str, Any]]:
    """Chargebacks that still need a response, soonest deadline first."""
    today = today or store_today()
    out = []
    for d in shopify.list_disputes():
        if d["status"] not in OPEN:
            continue
        due = _day(d.get("evidence_due_by"))
        out.append(
            {
                **d,
                "reason_label": REASONS.get(d["reason"], d["reason"]),
                "days_left": (due - today).days if due else None,
            }
        )
    return sorted(out, key=lambda d: (d["days_left"] is None, d["days_left"] or 0))
