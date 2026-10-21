"""Run the agent, keep its paused runs, and carry out a person's decision.

Each ticket run is a LangGraph thread. The graph pauses at its approval
interrupt; the checkpointer keeps the paused run (in Postgres in production),
so it survives restarts. A record per run in the LangGraph store (also in
Postgres) makes the queue easy to list.

When a person approves:
1. The refund, if there is one, is checked again against the order as it is now.
2. The paused run resumes, and records the decision on the ticket.
3. This service, not the agent, sends the reply and issues the refund.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from langgraph.types import Command

from copilot.agent.graph import AgentContext, build_graph
from copilot.agent.tools import in_memory_tools
from copilot.audit import AuditLog
from copilot.disputes.evidence import DisputeNotFound, build_packet, open_disputes
from copilot.rag.retriever import Retriever
from copilot.rag.server import build_server as build_policies
from copilot.shopify.backend import FakeShopify
from copilot.shopify.refunds import RefundError, check_refund_request
from copilot.shopify.server import build_server as build_shopify
from copilot.tracing import run_context
from copilot.zendesk.backend import ZendeskBackend
from copilot.zendesk.server import build_server as build_zendesk

NAMESPACE = ("approvals",)
DISPUTES = ("disputes",)
DISPUTE_DECISIONS = ("submitted", "accepted")
SUPPORT_SIGNATURE = "Northwind Coffee Support"


class ApprovalError(RuntimeError):
    """A decision that cannot be carried out. The message is shown to the reviewer."""


@contextmanager
def _traced(record: dict[str, Any], reviewer: str, approved: bool) -> Iterator[None]:
    with run_context(
        record["ticket_id"], record["thread_id"], step="decision", approved=approved, reviewer=reviewer
    ):
        yield


def _normalize(text: str) -> str:
    """Browsers send line breaks as CRLF; compare and store replies with plain newlines."""
    return text.replace("\r\n", "\n").strip()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Backends:
    """The store's systems. In this project, fakes loaded with the seed data, shared by every run."""

    zendesk: ZendeskBackend
    shopify: FakeShopify
    retriever: Retriever
    audit: io.StringIO = field(default_factory=io.StringIO)

    def servers(self) -> dict[str, Any]:
        return {
            "zendesk": lambda: build_zendesk(self.zendesk, audit=AuditLog("zendesk", self.audit)),
            "shopify": lambda: build_shopify(self.shopify, audit=AuditLog("shopify", self.audit)),
            "policies": lambda: build_policies(self.retriever, audit=AuditLog("policies", self.audit)),
        }


class ApprovalService:
    def __init__(
        self,
        backends: Backends,
        checkpointer: BaseCheckpointSaver,
        store: BaseStore,
        triage_model: Any,
        draft_model: Any,
    ) -> None:
        self.backends = backends
        self.graph = build_graph(checkpointer=checkpointer)
        self.store = store
        self.triage_model = triage_model
        self.draft_model = draft_model
        self._locks: dict[str, asyncio.Lock] = {}

    def _context(self, tools: Any, reviewer: str = "staff") -> AgentContext:
        return AgentContext(
            tools=tools, triage_model=self.triage_model, draft_model=self.draft_model, reviewer_name=reviewer
        )

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    # Running the agent

    async def start(self, ticket_id: int) -> dict[str, Any]:
        """Run the agent on a ticket until it needs a person. Returns the queue record."""
        thread_id = f"ticket-{ticket_id}-{uuid.uuid4().hex[:8]}"
        async with in_memory_tools(self.backends.servers()) as tools:
            with run_context(ticket_id, thread_id, step="draft"):
                state = await self.graph.ainvoke(
                    {"ticket_id": ticket_id}, self._config(thread_id), context=self._context(tools)
                )
        if state.get("status") == "failed":
            raise ApprovalError(state.get("error") or "The agent could not run on this ticket.")

        ticket = state["ticket"]
        customer = (state.get("evidence") or {}).get("customer") or {}
        risk_level = (customer.get("risk") or {}).get("level", "low")
        record = {
            "thread_id": thread_id,
            "ticket_id": ticket_id,
            "subject": ticket["subject"],
            "customer": ticket["requester"]["name"],
            "intent": state["triage"]["intent"],
            "action": state["action"],
            "summary": state["draft"]["summary"],
            "reason": state["decision"]["reason"],
            "refund_amount": (state.get("proposal") or {}).get("amount"),
            "flags": len(state.get("flags", [])),
            "risks": [
                label
                for label, on in (
                    ("possible prompt injection", state["triage"].get("injection")),
                    ("chargeback risk", state["triage"].get("chargeback_risk")),
                    (f"{risk_level} risk customer", risk_level in ("medium", "high")),
                )
                if on
            ],
            "status": "escalated" if state["status"] == "escalated" else "pending",
            "created_at": _now(),
            "decided_at": None,
            "reviewer": None,
        }
        await self.store.aput(NAMESPACE, thread_id, record)
        return record

    # The queue

    async def has_run(self, ticket_id: int) -> bool:
        """Whether the copilot has already run on this ticket (any outcome)."""
        found = await self.store.asearch(NAMESPACE, filter={"ticket_id": ticket_id}, limit=1)
        return bool(found)

    async def records(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        found = await self.store.asearch(
            NAMESPACE, filter={"status": status} if status else None, limit=limit
        )
        return sorted((item.value for item in found), key=lambda r: r["created_at"], reverse=True)

    async def detail(self, thread_id: str) -> dict[str, Any]:
        """The queue record plus the run's full state, for the review page."""
        item = await self.store.aget(NAMESPACE, thread_id)
        if item is None:
            raise ApprovalError(f"No run {thread_id}")
        snapshot = await self.graph.aget_state(self._config(thread_id))
        state = snapshot.values
        sources: dict[str, dict[str, Any]] = {}
        for p in (state.get("evidence") or {}).get("passages", []):
            if p["doc_id"] in state.get("citations", []):
                entry = sources.setdefault(
                    p["doc_id"], {"title": p["title"], "url": p["url"], "sections": []}
                )
                if p.get("section") and p["section"] not in entry["sections"]:
                    entry["sections"].append(p["section"])
        return {
            "record": item.value,
            "state": state,
            "sources": list(sources.values()),
            "waiting": snapshot.next == ("approval",),
        }

    # Deciding

    async def decide(
        self, thread_id: str, *, approved: bool, reviewer: str, reply: str | None = None
    ) -> dict[str, Any]:
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            info = await self.detail(thread_id)
            record, state = info["record"], info["state"]
            if record["status"] != "pending" or not info["waiting"]:
                raise ApprovalError(f"This run is {record['status']}, not waiting for a decision.")
            drafted = _normalize(state["draft"]["reply"])
            reply = _normalize(reply or "") or drafted
            if approved and not reply:
                raise ApprovalError("The reply is empty.")

            proposal = state.get("proposal")
            if approved and proposal:
                self._recheck_refund(state, proposal)  # raises before anything changes

            async with in_memory_tools(self.backends.servers()) as tools:
                with _traced(record, reviewer, approved):
                    await self.graph.ainvoke(
                        Command(resume={"approved": approved, "reviewer": reviewer, "reply": reply}),
                        self._config(thread_id),
                        context=self._context(tools, reviewer),
                    )

            ticket_id = record["ticket_id"]
            outcome: dict[str, Any] = {}
            if approved:
                if proposal:
                    refund = self.backends.shopify.issue_refund(
                        proposal["order_name"],
                        Decimal(proposal["amount"]),
                        note=f"Ticket {ticket_id}: {proposal['reason']}, approved by {reviewer}",
                        proposal_id=proposal["id"],
                    )
                    outcome["refund"] = refund
                self.backends.zendesk.add_public_reply(ticket_id, reply, author=SUPPORT_SIGNATURE)
                self.backends.zendesk.set_status(ticket_id, "solved")
                outcome["reply_sent"] = True
            else:
                if proposal:
                    self.backends.shopify.close_proposal(proposal["id"], "rejected")
                self.backends.zendesk.set_status(ticket_id, "open")  # back to a person to handle

            record |= {
                "status": "approved" if approved else "rejected",
                "decided_at": _now(),
                "reviewer": reviewer,
                "reply_edited": approved and reply != drafted,
            }
            await self.store.aput(NAMESPACE, thread_id, record)
            return {"record": record, **outcome}

    def _recheck_refund(self, state: dict[str, Any], proposal: dict[str, Any]) -> None:
        refund = (state.get("decision") or {}).get("refund") or {}
        try:
            check_refund_request(
                self.backends.shopify,
                order_name=proposal["order_name"],
                amount=proposal["amount"],
                reason=proposal["reason"],
                line_item_id=proposal.get("line_item_id"),
                opened=refund.get("opened"),
                problem=refund.get("problem"),
                exclude_proposal_id=proposal["id"],
            )
        except RefundError as e:
            raise ApprovalError(f"The refund can no longer be issued: {e}") from None

    # Chargebacks

    async def dispute_decisions(self) -> dict[int, dict[str, Any]]:
        found = await self.store.asearch(DISPUTES, limit=500)
        return {item.value["dispute_id"]: item.value for item in found}

    async def disputes(self) -> list[dict[str, Any]]:
        """Open chargebacks, soonest deadline first, with any decision already recorded here."""
        decided = await self.dispute_decisions()
        out = []
        for d in open_disputes(self.backends.shopify):
            packet = build_packet(self.backends.shopify, self.backends.zendesk, d["id"])
            out.append(
                d | {"decision": decided.get(d["id"]), "recommendation": packet["recommendation"]["action"]}
            )
        return out

    async def dispute(self, dispute_id: int) -> dict[str, Any]:
        try:
            packet = build_packet(self.backends.shopify, self.backends.zendesk, dispute_id)
        except DisputeNotFound as e:
            raise ApprovalError(str(e)) from None
        return {
            "packet": packet,
            "fields_json": json.dumps(packet["shopify_fields"], indent=2, ensure_ascii=False),
            "decision": (await self.dispute_decisions()).get(dispute_id),
        }

    async def decide_dispute(self, dispute_id: int, *, decision: str, reviewer: str) -> dict[str, Any]:
        """Record that a person submitted the evidence (in Shopify) or accepted the chargeback.

        Nothing is sent to the bank from here: the person submits the evidence themselves."""
        if decision not in DISPUTE_DECISIONS:
            raise ApprovalError(f"decision must be one of {', '.join(DISPUTE_DECISIONS)}")
        info = await self.dispute(dispute_id)
        if info["decision"]:
            raise ApprovalError(f"Chargeback {dispute_id} was already marked {info['decision']['decision']}.")
        if not info["packet"]["dispute"]["open"]:
            raise ApprovalError(f"Chargeback {dispute_id} is not open.")
        record = {
            "dispute_id": dispute_id,
            "decision": decision,
            "recommended": info["packet"]["recommendation"]["action"],
            "reviewer": reviewer,
            "decided_at": _now(),
        }
        await self.store.aput(DISPUTES, str(dispute_id), record)
        return record
