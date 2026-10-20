"""The agent as a LangGraph workflow.

    load_ticket -> triage -> gather -> decide -> draft -> write_back -> approval
                   (model)    (tools)  (code)   (model     (tools)      (person)
                                                 + code checks)

A person approves every customer-visible reply and every refund. The graph
pauses at `approval` with an interrupt; with a checkpointer, the paused run
survives until someone resumes it with their decision. Escalations skip the
approval step and go to the team's queue with an internal note.

State holds plain JSON-like values only, so any checkpointer can store it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from copilot.agent import prompts
from copilot.agent.rules import (
    Decision,
    Evidence,
    RefundPlan,
    decide,
    guard,
    needs_item_check,
    order_name_from,
    pick_line_item,
)
from copilot.agent.schemas import Action, Draft, Intent, Triage
from copilot.agent.tools import StoreTools, ToolCallError

MAX_PASSAGES = 8
DEFAULT_QUERIES = {
    Intent.DUPLICATE_CHARGE: "charged twice duplicate or pending charge",
    Intent.UNRECOGNIZED_CHARGE: "charge name on my bank statement",
    Intent.REFUND_REQUEST: "refund policy returns",
    Intent.REFUND_STATUS: "how long refunds take to appear",
    Intent.DAMAGED_OR_LOST: "damaged or lost order",
    Intent.ORDER_CHANGE: "change or cancel an order",
}
REFUND_INTENTS = (Intent.REFUND_REQUEST, Intent.DAMAGED_OR_LOST)


@dataclass
class AgentContext:
    tools: StoreTools
    triage_model: BaseChatModel
    draft_model: BaseChatModel
    reviewer_name: str = "staff"


class State(TypedDict, total=False):
    ticket_id: int
    ticket: dict[str, Any]
    triage: dict[str, Any]
    evidence: dict[str, Any]
    decision: dict[str, Any]
    draft: dict[str, Any]
    action: str
    citations: list[str]
    flags: list[str]
    proposal: dict[str, Any] | None
    approval: dict[str, Any] | None
    status: Literal["escalated", "awaiting_approval", "approved", "rejected", "failed"]
    error: str | None


def _evidence(state: State) -> Evidence:
    return Evidence(**state["evidence"])


def _decision(state: State) -> Decision:
    d = dict(state["decision"])
    refund = RefundPlan(**d["refund"]) if d.get("refund") else None
    return Decision(Action(d["action"]), d["reason"], refund)


async def load_ticket(state: State, runtime: Runtime[AgentContext]) -> State:
    try:
        ticket = await runtime.context.tools.call("get_ticket", ticket_id=state["ticket_id"])
    except ToolCallError as e:
        return {"status": "failed", "error": str(e)}
    return {"ticket": ticket, "flags": [], "error": None}


async def triage(state: State, runtime: Runtime[AgentContext]) -> State:
    model = runtime.context.triage_model.with_structured_output(Triage)
    result: Triage = await model.ainvoke(
        [SystemMessage(prompts.TRIAGE_SYSTEM), HumanMessage(prompts.ticket_block(state["ticket"]))]
    )
    # An order number written in the ticket is read by code; the model's reading is the fallback.
    text = " ".join(c.get("customer_text", "") for c in state["ticket"]["comments"])
    written = order_name_from(text)
    if written:
        result.order_name = written
    return {"triage": result.model_dump(mode="json")}


async def gather(state: State, runtime: Runtime[AgentContext]) -> State:
    tools = runtime.context.tools
    t = Triage(**state["triage"])
    email = state["ticket"]["requester"]["email"]
    ev = Evidence()

    ev.customer = await tools.try_call("get_customer", email=email)
    if t.order_name:
        ev.order = await tools.try_call("get_order", order_name=t.order_name)
        if ev.order is None:
            ev.notes.append(f"Order {t.order_name} was not found.")
        elif ev.order["email"].lower() != email.lower():
            ev.notes.append(f"Order {t.order_name} belongs to a different customer; not used.")
            ev.order = None

    if ev.order is None and t.intent is Intent.UNRECOGNIZED_CHARGE and t.charge_amount:
        charges = await tools.try_call("find_charges", amount=t.charge_amount) or []
        mine = {o["name"] for o in (ev.customer or {}).get("recent_orders", [])}
        ev.charges = [c for c in charges if c["order_name"] in mine]
        if ev.charges:
            ev.order = await tools.try_call("get_order", order_name=ev.charges[0]["order_name"])
        else:
            ev.notes.append(f"No charge of {t.charge_amount} on this customer's orders.")

    if ev.order is not None and needs_item_check(t):
        ev.line_item = pick_line_item(ev.order, t.item)
        if ev.line_item is not None:
            ev.eligibility = await tools.try_call(
                "check_refund_eligibility",
                order_name=ev.order["name"],
                line_item_id=ev.line_item["id"],
                opened=bool(t.opened),
                problem=t.problem or "none",
            )

    # Policy versions: for an order, use the policies in force when it was delivered.
    as_of = None
    if ev.order is not None and t.intent in REFUND_INTENTS and ev.order.get("delivered_at"):
        as_of = ev.order["delivered_at"]
    queries = list(dict.fromkeys([*t.questions[:3], DEFAULT_QUERIES.get(t.intent, "")]))
    seen: set[tuple[str, str | None]] = set()
    for q in (q for q in queries if q.strip()):
        found = await tools.try_call("search_policies", query=q, k=4, as_of=as_of)
        for p in (found or {}).get("results", []):
            key = (p["doc_id"], p.get("section"))
            if key not in seen and len(ev.passages) < MAX_PASSAGES:
                seen.add(key)
                ev.passages.append(p)

    history = await tools.try_call("get_customer_tickets", email=email, limit=5) or []
    ev.history = [
        {"id": h["id"], "subject": h["subject"], "status": h["status"], "created_at": h["created_at"]}
        for h in history
        if h["id"] != state["ticket_id"]
    ]
    return {"evidence": asdict(ev)}


async def decide_node(state: State) -> State:
    decision = decide(Triage(**state["triage"]), _evidence(state))
    return {"decision": asdict(decision) | {"action": decision.action.value}, "action": decision.action.value}


def _facts(ev: Evidence, decision: Decision) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if ev.order:
        o = ev.order
        facts["order"] = {
            "name": o["name"],
            "placed": o["created_at"][:10],
            "shipped": o.get("shipped_at"),
            "delivered": o.get("delivered_at"),
            "status": o["fulfillment_status"],
            "items": [
                {"title": i["title"], "price": i["price"], "quantity": i["quantity"]} for i in o["line_items"]
            ],
            "total": o["total"],
            "payments": {k: v for k, v in o["payments"].items() if v not in (None, "")},
            "charges": [
                {
                    "amount": t["amount"],
                    "status": t["status"],
                    "date": t["created_at"][:10],
                    "descriptor": t["descriptor"],
                }
                for t in o["transactions"]
                if t["kind"] == "sale"
            ],
            "refunds": [{"amount": r["amount"], "date": r["created_at"][:10]} for r in o["refunds"]],
        }
    if ev.eligibility:
        facts["refund_check"] = {k: ev.eligibility[k] for k in ("title", "outcome", "reason")}
    if decision.refund:
        facts["refund_amount"] = decision.refund.amount
    if ev.notes:
        facts["notes"] = ev.notes
    return facts


async def draft(state: State, runtime: Runtime[AgentContext]) -> State:
    ev, decision = _evidence(state), _decision(state)
    model = runtime.context.draft_model.with_structured_output(Draft)
    content = prompts.draft_input(
        state["ticket"], decision.action.value, decision.reason, _facts(ev, decision), ev.passages
    )
    result: Draft = await model.ainvoke([SystemMessage(prompts.DRAFT_SYSTEM), HumanMessage(content)])
    checked = guard(result, decision, ev, prompts=(prompts.DRAFT_SYSTEM, prompts.TRIAGE_SYSTEM))
    flags = list(state.get("flags", [])) + checked.flags
    proposal = None

    if checked.action is Action.PROPOSE_REFUND and decision.refund:
        r = decision.refund
        args: dict[str, Any] = {
            "order_name": ev.order["name"],
            "amount": r.amount,
            "reason": r.reason,
            "ticket_id": state["ticket_id"],
            "explanation": decision.reason,
        }
        if r.reason == "item_refund":
            args |= {"line_item_id": r.line_item_id, "opened": r.opened, "problem": r.problem}
        try:
            proposal = await runtime.context.tools.call("propose_refund", **args)
        except ToolCallError as e:
            # The Shopify server re-checks every proposal; if it refuses, a person decides.
            flags.append(f"Refund proposal was rejected by the store: {e.message}")
            checked.action = Action.ESCALATE

    return {
        "draft": result.model_dump(mode="json"),
        "action": checked.action.value,
        "citations": checked.citations,
        "flags": flags,
        "proposal": proposal,
    }


def _note(state: State) -> str:
    t, d, dr = state["triage"], state["decision"], state["draft"]
    lines = [
        f"Support copilot: {state['action'].replace('_', ' ')}",
        f"Summary: {dr['summary']}",
        f"Intent: {t['intent']}" + (f", order {t['order_name']}" if t.get("order_name") else ""),
        f"Decision: {d['reason']}",
    ]
    if state.get("proposal"):
        p = state["proposal"]
        lines.append(f"Refund proposal #{p['id']}: ${p['amount']} ({p['reason']}), pending approval.")
    if state.get("citations"):
        lines.append("Sources: " + ", ".join(state["citations"]))
    for flag in state.get("flags", []):
        lines.append(f"Check: {flag}")
    if state["action"] != Action.ESCALATE:
        lines += ["", "Draft reply (not sent; needs approval):", dr["reply"]]
    return "\n".join(lines)


async def write_back(state: State, runtime: Runtime[AgentContext]) -> State:
    tools = runtime.context.tools
    t = state["triage"]
    ticket_id = state["ticket_id"]
    await tools.call("add_internal_note", ticket_id=ticket_id, body=_note(state))
    tags = ["copilot", f"intent:{t['intent']}", f"copilot:{state['action']}"]
    if t.get("chargeback_risk"):
        tags.append("chargeback_risk")
    if t.get("injection"):
        tags.append("possible_injection")
    await tools.call("add_tags", ticket_id=ticket_id, tags=tags)
    if t.get("chargeback_risk") or state["action"] == Action.ESCALATE:
        await tools.call("set_priority", ticket_id=ticket_id, priority="high")
    if state["action"] == Action.ESCALATE:
        return {"status": "escalated"}
    return {"status": "awaiting_approval"}


async def approval(state: State, runtime: Runtime[AgentContext]) -> State:
    """Pause until a person approves or rejects. Resume with Command(resume={...})."""
    answer = interrupt(
        {
            "ticket_id": state["ticket_id"],
            "action": state["action"],
            "reply": state["draft"]["reply"],
            "proposal": state.get("proposal"),
            "flags": state.get("flags", []),
        }
    )
    approved = bool(answer.get("approved"))
    reviewer = answer.get("reviewer") or runtime.context.reviewer_name
    reply = answer.get("reply") or state["draft"]["reply"]
    verdict = "approved" if approved else "rejected"
    body = f"Copilot draft {verdict} by {reviewer}."
    if approved and reply != state["draft"]["reply"]:
        body += " The reply was edited before sending."
    await runtime.context.tools.call("add_internal_note", ticket_id=state["ticket_id"], body=body)
    return {"status": verdict, "approval": {"approved": approved, "reviewer": reviewer, "reply": reply}}


def _after_load(state: State) -> str:
    return END if state.get("status") == "failed" else "triage"


def _after_write_back(state: State) -> str:
    return END if state["status"] == "escalated" else "approval"


def build_graph(checkpointer: BaseCheckpointSaver | None = None):  # noqa: ANN201
    g = StateGraph(State, context_schema=AgentContext)
    g.add_node("load_ticket", load_ticket)
    g.add_node("triage", triage)
    g.add_node("gather", gather)
    g.add_node("decide", decide_node)
    g.add_node("draft", draft)
    g.add_node("write_back", write_back)
    g.add_node("approval", approval)
    g.add_edge(START, "load_ticket")
    g.add_conditional_edges("load_ticket", _after_load, ["triage", END])
    g.add_edge("triage", "gather")
    g.add_edge("gather", "decide")
    g.add_edge("decide", "draft")
    g.add_edge("draft", "write_back")
    g.add_conditional_edges("write_back", _after_write_back, ["approval", END])
    g.add_edge("approval", END)
    return g.compile(checkpointer=checkpointer)
