"""The agent's decisions, in code.

The model reads the ticket (triage) and writes the reply (draft). Everything
between those two steps that decides what actually happens to the customer's
money is here, as plain functions that are easy to test and to read:

- which action to take for each kind of ticket;
- which order line a request is about;
- how much a refund proposal is for;
- the checks a draft must pass before a person sees it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from copilot.agent.schemas import Action, Draft, Intent, Triage
from copilot.privacy import find_personal_data
from copilot.store import today as store_today

ORDER_NAME = re.compile(r"#\s?(\d{4,})")
# A marker placed in the system prompts. If it ever shows up in a reply, the prompt leaked.
CANARY = "NWC-REF-7F3A"
REDACTION_TOKEN = re.compile(r"\[(?:EMAIL|PHONE|CARD|CARD_LAST4|NAME|ADDRESS|IP):[0-9a-f]{8}\]")
MONEY = re.compile(r"\$\s?(\d+(?:,\d{3})*(?:\.\d{2})?)")
LOST_IN_TRANSIT_DAYS = 7
TOOL_NAMES = (
    "search_tickets",
    "get_ticket",
    "get_customer_tickets",
    "add_internal_note",
    "add_tags",
    "set_priority",
    "get_order",
    "get_customer",
    "find_charges",
    "check_refund_eligibility",
    "propose_refund",
    "search_policies",
    "get_policy",
    "list_policies",
)


@dataclass
class RefundPlan:
    amount: str
    reason: str  # "duplicate_charge" or "item_refund"
    line_item_id: int | None = None
    opened: bool | None = None
    problem: str | None = None


@dataclass
class Decision:
    action: Action
    reason: str
    refund: RefundPlan | None = None


@dataclass
class Evidence:
    """What the agent looked up. Every field is optional: lookups can find nothing."""

    order: dict[str, Any] | None = None
    customer: dict[str, Any] | None = None
    charges: list[dict[str, Any]] = field(default_factory=list)
    line_item: dict[str, Any] | None = None
    eligibility: dict[str, Any] | None = None
    passages: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def order_name_from(text: str) -> str | None:
    """An order number written in the ticket. Code first; the model's reading is the fallback."""
    match = ORDER_NAME.search(text)
    return f"#{match.group(1)}" if match else None


def pick_line_item(order: dict[str, Any], item: str | None) -> dict[str, Any] | None:
    """The line the customer means: the one whose title they name, or the only one."""
    items = order.get("line_items", [])
    if len(items) == 1:
        return items[0]
    if not item:
        return None
    wanted = item.lower().strip()
    named = [i for i in items if i["title"].lower() in wanted or wanted in i["title"].lower()]
    if len(named) == 1:
        return named[0]
    words = set(re.findall(r"[a-z]+", wanted)) - {"the", "a", "my", "oz", "lb"}
    scored = [(len(words & set(re.findall(r"[a-z]+", i["title"].lower()))), i) for i in items]
    scored.sort(key=lambda s: -s[0])
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return None


def is_lost_parcel(t: Triage) -> bool:
    """A damaged-or-lost ticket about a parcel that has not arrived, not about an item in it."""
    return t.intent is Intent.DAMAGED_OR_LOST and t.problem in (None, "none") and not t.item


def needs_item_check(t: Triage) -> bool:
    return t.intent in (Intent.REFUND_REQUEST, Intent.DAMAGED_OR_LOST) and not is_lost_parcel(t)


def _pending_proposals(order: dict[str, Any]) -> Decimal:
    return sum(
        (
            Decimal(p["amount"])
            for p in order.get("refund_proposals", [])
            if p["status"] == "pending_approval"
        ),
        Decimal("0"),
    )


def decide(triage: Triage, ev: Evidence, *, today: date | None = None) -> Decision:
    decision = _decide(triage, ev, today or store_today())
    if triage.injection and decision.action is Action.PROPOSE_REFUND:
        # Defense in depth: a ticket that tries to steer the assistant never gets an
        # automatic refund proposal, even if the rules would allow one.
        return Decision(
            Action.ESCALATE, "Possible prompt injection on a refund request; a person should review."
        )
    return decision


def _decide(t: Triage, ev: Evidence, today: date) -> Decision:
    order = ev.order
    intent = t.intent

    if intent is Intent.DUPLICATE_CHARGE:
        if order is None:
            return Decision(Action.ESCALATE, "Could not find the order the charges belong to.")
        pay = order["payments"]
        excess = Decimal(pay["overcharged"]) - Decimal(pay["refunded"]) - _pending_proposals(order)
        if pay["duplicate_capture"] and excess > 0:
            return Decision(
                Action.PROPOSE_REFUND,
                f"Order {order['name']} was captured {pay['successful_charges']} times; "
                f"{excess:.2f} was charged in excess.",
                RefundPlan(amount=f"{excess:.2f}", reason="duplicate_charge"),
            )
        if pay["failed_attempts"]:
            return Decision(
                Action.ANSWER,
                "Only one charge was collected; the other was a failed attempt (a pending hold).",
            )
        return Decision(Action.ANSWER, "The order was charged once.")

    if intent is Intent.UNRECOGNIZED_CHARGE:
        if order is not None:
            return Decision(Action.ANSWER, f"The charge matches the customer's order {order['name']}.")
        return Decision(Action.ESCALATE, "No order matches this customer and amount; possible fraud.")

    if intent is Intent.REFUND_STATUS:
        if order is None:
            return Decision(Action.ESCALATE, "Could not find the order.")
        return Decision(Action.ANSWER, "Report the refund status from the order's records.")

    if intent is Intent.ORDER_CHANGE:
        if order is None:
            return Decision(Action.ESCALATE, "Could not find the order.")
        if order.get("shipped_at") is None:
            return Decision(Action.ESCALATE, "Order has not shipped yet; a person can still change it.")
        return Decision(Action.ANSWER, "Order has already shipped and cannot be changed.")

    if is_lost_parcel(t):
        if order is None:
            return Decision(Action.ESCALATE, "Could not find the order.")
        if order.get("delivered_at"):
            return Decision(
                Action.ANSWER, "Marked delivered; ask the customer to check around and wait 48 hours."
            )
        shipped = order.get("shipped_at")
        if shipped and today - date.fromisoformat(shipped) > timedelta(days=LOST_IN_TRANSIT_DAYS):
            return Decision(
                Action.ESCALATE,
                f"In transit since {shipped}, more than {LOST_IN_TRANSIT_DAYS} days; treat as lost and send a replacement.",
            )
        return Decision(Action.ANSWER, "Still within normal delivery time.")

    if intent in (Intent.REFUND_REQUEST, Intent.DAMAGED_OR_LOST):
        if order is None:
            return Decision(Action.ESCALATE, "Could not find the order.")
        if ev.line_item is None or ev.eligibility is None:
            return Decision(Action.ESCALATE, "Could not tell which item the request is about.")
        outcome = ev.eligibility["outcome"]
        if outcome == "refund":
            return Decision(
                Action.PROPOSE_REFUND,
                ev.eligibility["reason"],
                RefundPlan(
                    amount=ev.eligibility["max_amount"],
                    reason="item_refund",
                    line_item_id=ev.line_item["id"],
                    opened=bool(t.opened),
                    problem=t.problem or "none",
                ),
            )
        if outcome == "warranty":
            return Decision(
                Action.ESCALATE, ev.eligibility["reason"] + " Arrange a warranty repair or replacement."
            )
        return Decision(Action.DECLINE_REFUND, ev.eligibility["reason"])

    return Decision(Action.ANSWER, "Answer from the store's policies and help articles.")


@dataclass
class GuardResult:
    citations: list[str]
    flags: list[str]
    action: Action


def allowed_amounts(ev: Evidence, decision: Decision) -> set[str]:
    """Every dollar amount the reply may mention: ones that appear in the evidence."""
    texts = [str(ev.order or ""), str(ev.charges), str(ev.eligibility or "")]
    texts += [p.get("text", "") for p in ev.passages]
    found = {m.replace(",", "") for t in texts for m in re.findall(r"\d+(?:,\d{3})*\.\d{2}|\d+", t)}
    if decision.refund:
        found.add(decision.refund.amount)
    normalized = set()
    for f in found:
        normalized.add(f)
        if "." in f and f.endswith(".00"):
            normalized.add(f[:-3])
        elif "." not in f:
            normalized.add(f + ".00")
    return normalized


URL = re.compile(r"\b(?:https?://|www\.)[^\s<>()\"']+", re.IGNORECASE)
STORE_URL = re.compile(r"^(?:https?://)?(?:www\.)?northwind-coffee\.example(?:[/?#]|$)", re.IGNORECASE)
# Sentences end at . ! or ? followed by a space or the end, so "$22.95" is not split in two.
SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
# A claim that a refund already happened: past or perfect tense, not a policy statement
# ("refunds are issued within 7 days") and not a negation ("no refund has been issued").
REFUND_DONE = re.compile(
    r"\b(?:(?:has|have|had)\s+(?:now\s+|already\s+|just\s+)?been|was|were|we(?:'ve| have)?|i(?:'ve| have))\s+"
    r"(?:now\s+|already\s+|just\s+)?(?:issued|processed|refunded|sent|credited|approved|completed)\b"
    r"|\bis\s+(?:now\s+)?on\s+its\s+way\b",
    re.IGNORECASE,
)
NEGATION = re.compile(
    r"\b(?:no|not|never|hasn't|haven't|hadn't|wasn't|weren't|isn't|cannot|can't|won't)\b", re.I
)


def _ngrams(text: str, n: int = 8) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _refund_claim_problems(reply: str, decision: Decision, ev: Evidence) -> list[str]:
    """A reply may only say a refund happened if one is proposed, or already exists on the order,
    and only for that amount."""
    valid = {decision.refund.amount} if decision.refund else set()
    for r in (ev.order or {}).get("refunds", []):
        valid.add(r["amount"])
    problems = []
    for sentence in SENTENCE_END.split(reply):
        if "refund" not in sentence.lower() or not REFUND_DONE.search(sentence) or NEGATION.search(sentence):
            continue
        if not valid:
            problems.append("Reply says a refund was made, but no refund is proposed or on record")
            continue
        amounts = {m.replace(",", "") for m in MONEY.findall(sentence)}
        amounts = {a if "." in a else f"{a}.00" for a in amounts}
        wrong = sorted(amounts - valid)
        if wrong:
            problems.append(
                f"Reply states a refund of {', '.join('$' + w for w in wrong)}, "
                f"but the refund is ${', $'.join(sorted(valid))}"
            )
    return list(dict.fromkeys(problems))


def guard(draft: Draft, decision: Decision, ev: Evidence, *, prompts: tuple[str, ...] = ()) -> GuardResult:
    """Checks on the model's draft.

    Problems become flags that the reviewer sees. Some are serious enough that the draft must
    not be sent at all, so the ticket is escalated instead: a reply that leaks the prompt,
    repeats personal data, links outside the store, or misstates a refund. These are what a
    successful prompt injection would produce, so they are checked in code, whatever the
    model says about the ticket.
    """
    flags: list[str] = []
    blocking: list[str] = []
    reply = draft.reply
    retrieved = {p["doc_id"] for p in ev.passages}
    citations = [c for c in dict.fromkeys(draft.citations) if c in retrieved]
    dropped = [c for c in draft.citations if c not in retrieved]
    if dropped:
        flags.append(f"Removed citations to documents that were not retrieved: {', '.join(dropped)}")

    allowed = allowed_amounts(ev, decision)
    unsupported = sorted({m.replace(",", "") for m in MONEY.findall(reply)} - allowed)
    if unsupported:
        flags.append(
            f"Reply mentions amounts not found in the evidence: {', '.join('$' + u for u in unsupported)}"
        )

    personal = find_personal_data(reply)
    if REDACTION_TOKEN.search(reply):
        personal.append("a redaction token")
    if personal:
        blocking.append("Reply contains " + ", ".join(personal))

    leaked = [name for name in TOOL_NAMES if name in reply]
    if leaked:
        blocking.append(f"Reply mentions internal tool names: {', '.join(leaked)}")

    if CANARY in reply or any(_ngrams(reply) & _ngrams(p) for p in prompts):
        blocking.append("Reply repeats the assistant's instructions")

    passage_text = " ".join(p.get("text", "") for p in ev.passages)
    links = [u.rstrip(".,;:!?") for u in URL.findall(reply)]
    foreign = [u for u in links if not STORE_URL.match(u) and u not in passage_text]
    if foreign:
        blocking.append(f"Reply links outside the store: {', '.join(foreign)}")

    blocking += _refund_claim_problems(reply, decision, ev)

    action = decision.action
    if action is Action.ANSWER and not draft.covered:
        action = Action.ESCALATE
        flags.append("The policies retrieved do not answer the question; escalated instead of answering.")
    if blocking:
        action = Action.ESCALATE
        flags += blocking
        flags.append("Draft not sent: escalated for a person to reply.")
    return GuardResult(citations=citations, flags=flags, action=action)
