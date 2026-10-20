"""The agent: its rules in code, and full runs of the graph against in-process MCP servers.

Models are scripted here (see copilot.agent.scripted), so these tests need no API key.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from copilot.agent import evaluate as agent_eval
from copilot.agent.graph import AgentContext, build_graph
from copilot.agent.rules import Decision, Evidence, decide, guard, order_name_from, pick_line_item
from copilot.agent.schemas import Action, Draft, Intent, Triage
from copilot.agent.scripted import AdversarialModel, OracleModel, load_eval_cases
from copilot.agent.tools import in_memory_tools
from copilot.kb import load_kb
from copilot.rag.retriever import Retriever

CASES = load_eval_cases(agent_eval.DATA / "eval_tickets.jsonl")


def order(**overrides):
    base = {
        "name": "#1000",
        "email": "a@example.com",
        "shipped_at": "2026-10-01",
        "delivered_at": "2026-10-04",
        "fulfillment_status": "fulfilled",
        "line_items": [{"id": 1, "title": "House Blend 12 oz", "price": "16.00", "quantity": 1}],
        "refund_proposals": [],
        "payments": {
            "duplicate_capture": False,
            "overcharged": "0.00",
            "refunded": "0.00",
            "failed_attempts": 0,
            "successful_charges": 1,
        },
    }
    return base | overrides


# Rules


def test_order_number_is_read_by_code():
    assert order_name_from("my order #1792 arrived") == "#1792"
    assert order_name_from("order # 1792") == "#1792"
    assert order_name_from("no order here, $45.35") is None


def test_pick_line_item():
    two = order(
        line_items=[
            {"id": 1, "title": "House Blend 12 oz", "price": "16.00", "quantity": 1},
            {"id": 2, "title": "Gooseneck Kettle 1L", "price": "79.00", "quantity": 1},
        ]
    )
    assert pick_line_item(two, "the kettle")["id"] == 2
    assert pick_line_item(two, "House Blend 12 oz")["id"] == 1
    assert pick_line_item(two, None) is None
    assert pick_line_item(order(), None)["id"] == 1


def test_duplicate_charge_refunds_only_the_excess():
    o = order(
        payments={
            "duplicate_capture": True,
            "overcharged": "45.35",
            "refunded": "0.00",
            "failed_attempts": 0,
            "successful_charges": 2,
        }
    )
    d = decide(Triage(intent=Intent.DUPLICATE_CHARGE), Evidence(order=o))
    assert d.action is Action.PROPOSE_REFUND and d.refund.amount == "45.35"
    pending = o | {"refund_proposals": [{"amount": "45.35", "status": "pending_approval"}]}
    assert decide(Triage(intent=Intent.DUPLICATE_CHARGE), Evidence(order=pending)).action is Action.ANSWER


def test_injection_never_gets_a_refund_proposal():
    elig = {"outcome": "refund", "reason": "ok", "max_amount": "16.00"}
    ev = Evidence(order=order(), line_item=order()["line_items"][0], eligibility=elig)
    assert decide(Triage(intent=Intent.REFUND_REQUEST), ev).action is Action.PROPOSE_REFUND
    assert decide(Triage(intent=Intent.REFUND_REQUEST, injection=True), ev).action is Action.ESCALATE


def test_lost_parcel_escalates_after_a_week_in_transit():
    lost = Triage(intent=Intent.DAMAGED_OR_LOST)
    in_transit = order(shipped_at="2026-09-30", delivered_at=None)
    assert decide(lost, Evidence(order=in_transit), today=date(2026, 10, 15)).action is Action.ESCALATE
    assert decide(lost, Evidence(order=in_transit), today=date(2026, 10, 3)).action is Action.ANSWER
    assert decide(lost, Evidence(order=order())).action is Action.ANSWER  # marked delivered


def test_guard_flags_made_up_amounts_sources_and_tool_names():
    ev = Evidence(order=order(), passages=[{"doc_id": "refund-policy", "text": "Refunds within 30 days."}])
    draft = Draft(
        reply="Your $16.00 order is eligible. Also $500 credit! (via get_order)",
        citations=["refund-policy", "made-up-doc"],
        summary="x",
    )
    result = guard(draft, Decision(Action.ANSWER, "x"), ev)
    assert result.citations == ["refund-policy"]
    assert any("made-up-doc" in f for f in result.flags)
    assert any("$500" in f for f in result.flags) and not any("$16.00" in f for f in result.flags)
    assert any("get_order" in f for f in result.flags)


def test_uncovered_answer_is_escalated():
    draft = Draft(reply="Not sure.", covered=False, summary="x")
    assert guard(draft, Decision(Action.ANSWER, "x"), Evidence()).action is Action.ESCALATE


# Full runs of the graph


def _servers():
    return agent_eval._servers(Retriever(load_kb(agent_eval.KB_DIR)))


def test_oracle_reaches_every_labeled_action():
    """With a perfect reading of each ticket, tools + rules + guards must match every label."""
    args = argparse.Namespace(model="oracle", split="all", limit=None, judge=False, rag_models=False)
    summary, results = asyncio.run(agent_eval.evaluate(args))
    wrong = [
        (r.case_id, r.expected_action, r.action, r.error) for r in results if r.action != r.expected_action
    ]
    assert not wrong
    assert summary.refund_amount_accuracy == 1.0
    assert summary.unsafe == 0


def test_compromised_model_stays_within_the_rails():
    """A model that lies about every ticket still cannot get an out-of-policy refund or an unflagged reply."""
    args = argparse.Namespace(model="adversarial", split="all", limit=None, judge=False, rag_models=False)
    summary, results = asyncio.run(agent_eval.evaluate(args))
    assert summary.errors == 0
    assert summary.unsafe == 0, [(r.case_id, r.unsafe) for r in results if r.unsafe]
    drafted = [r for r in results if r.reply]
    assert drafted and all(any("$9999" in f for f in r.flags) for r in drafted)


@pytest.mark.parametrize(("approved", "status"), [(True, "approved"), (False, "rejected")])
def test_approval_pauses_and_resumes(approved, status):
    async def scenario():
        model = OracleModel(CASES)
        graph = build_graph(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": f"t-{approved}"}}
        async with in_memory_tools(_servers()) as tools:
            ctx = AgentContext(tools=tools, triage_model=model, draft_model=model, reviewer_name="sam")
            paused = await graph.ainvoke({"ticket_id": 10301}, config, context=ctx)
            assert paused["status"] == "awaiting_approval"
            assert paused["__interrupt__"][0].value["proposal"]["amount"] == "45.35"

            ticket = await tools.call("get_ticket", ticket_id=10301)
            note = ticket["comments"][-1]
            assert note["public"] is False and "Refund proposal #1: $45.35" in note["body"]
            assert "copilot:propose_refund" in ticket["tags"]

            done = await graph.ainvoke(Command(resume={"approved": approved}), config, context=ctx)
            assert done["status"] == status
            ticket = await tools.call("get_ticket", ticket_id=10301)
            assert ticket["comments"][-1]["body"].startswith(f"Copilot draft {status} by sam")
            # Nothing customer-visible was written by the agent at any point.
            assert [c for c in ticket["comments"] if c["public"]] == ticket["comments"][:1]

    asyncio.run(scenario())


def test_escalation_skips_approval():
    async def scenario():
        model = OracleModel(CASES)
        graph = build_graph()
        async with in_memory_tools(_servers()) as tools:
            ctx = AgentContext(tools=tools, triage_model=model, draft_model=model)
            state = await graph.ainvoke(
                {"ticket_id": 10313}, context=ctx
            )  # eval-013: unknown charge, possible fraud
            assert state["status"] == "escalated" and "__interrupt__" not in state
            ticket = await tools.call("get_ticket", ticket_id=10313)
            assert ticket["priority"] == "high" and "chargeback_risk" in ticket["tags"]

    asyncio.run(scenario())


def test_unknown_ticket_fails_cleanly():
    async def scenario():
        model = AdversarialModel()
        async with in_memory_tools(_servers()) as tools:
            ctx = AgentContext(tools=tools, triage_model=model, draft_model=model)
            state = await build_graph().ainvoke({"ticket_id": 1}, context=ctx)
        assert state["status"] == "failed" and "not found" in state["error"]
        assert model.calls == []  # the model is never called for a ticket that does not exist

    asyncio.run(scenario())


def test_errors_are_unwrapped_from_exception_groups():
    from copilot.agent.evaluate import describe_error

    inner = ValueError("Error code: 400 - model not found")
    wrapped = ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("inner", [inner])])
    assert describe_error(wrapped) == "ValueError: Error code: 400 - model not found"
