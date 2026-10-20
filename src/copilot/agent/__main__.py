"""Run the support agent on a ticket from the command line.

    python -m copilot.agent tickets                 # list new tickets
    python -m copilot.agent run 10301               # triage, look things up, draft, then ask you to approve
    python -m copilot.agent run 10301 --rag-models  # use dense search and the reranker for policies
    python -m copilot.agent run 10301 --model oracle  # offline: read the eval labels instead of calling Claude

The three MCP servers start as subprocesses over stdio, on the fake backends.
Their data lives in memory, so notes and proposals last for this run only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from copilot.agent.graph import AgentContext, build_graph
from copilot.agent.tools import stdio_tools
from copilot.tracing import run_context, setup_tracing, shutdown_tracing

ROOT = Path(__file__).resolve().parents[3]


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Read KEY=value lines from .env into the environment, without overriding what is already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _models(name: str) -> tuple[Any, Any]:
    if name == "oracle":
        from copilot.agent.scripted import OracleModel, load_eval_cases

        m = OracleModel(load_eval_cases(ROOT / "data" / "generated" / "eval_tickets.jsonl"))
        return m, m
    from copilot.agent.models import draft_model, require_api_key, triage_model

    require_api_key()
    return triage_model(), draft_model()


def _print_block(title: str, body: str) -> None:
    print(f"\n== {title}")
    print(textwrap.indent(body.strip(), "  "))


async def list_tickets() -> None:
    async with stdio_tools() as tools:
        tickets = await tools.call("search_tickets", query="", status="new", limit=50)
    for t in sorted(tickets, key=lambda t: t["id"]):
        preview = t["preview"]["customer_text"].replace("\n", " ")[:80]
        print(f"{t['id']}  {t['subject'][:40]:<40}  {preview}")


async def run(args: argparse.Namespace) -> int:
    triage_m, draft_m = _models(args.model)
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": f"ticket-{args.ticket_id}-{uuid.uuid4().hex[:8]}"}}

    async with stdio_tools(rag_models=args.rag_models) as tools:
        ctx = AgentContext(
            tools=tools, triage_model=triage_m, draft_model=draft_m, reviewer_name=args.reviewer
        )
        with run_context(args.ticket_id, config["configurable"]["thread_id"], step="draft"):
            state = await graph.ainvoke({"ticket_id": args.ticket_id}, config, context=ctx)
        if state.get("status") == "failed":
            print(f"Could not run: {state.get('error')}", file=sys.stderr)
            return 1

        ticket = state["ticket"]
        customer = "\n\n".join(c["customer_text"] for c in ticket["comments"] if "customer_text" in c)
        _print_block(f"Ticket {ticket['id']}: {ticket['subject']}", customer)
        t = state["triage"]
        _print_block(
            "Triage",
            f"intent {t['intent']}, order {t.get('order_name') or '-'}"
            + (", chargeback risk" if t.get("chargeback_risk") else "")
            + (", POSSIBLE PROMPT INJECTION" if t.get("injection") else ""),
        )
        _print_block("Decision", f"{state['action']}: {state['decision']['reason']}")
        if state.get("proposal"):
            p = state["proposal"]
            _print_block(
                "Refund proposal", f"#{p['id']} for ${p['amount']} ({p['reason']}), pending approval"
            )
        if state.get("citations"):
            _print_block("Sources", "\n".join(state["citations"]))
        if state.get("flags"):
            _print_block("Checks", "\n".join(state["flags"]))
        if args.verbose:
            _print_block("Tool calls", "\n".join(json.dumps(c) for c in tools.calls))

        if state["status"] == "escalated":
            _print_block(
                "Escalated",
                "Sent to the team's queue with an internal note. Nothing was sent to the customer.",
            )
            return 0

        _print_block("Draft reply (not sent)", state["draft"]["reply"])
        if args.approve is None:
            answer = input("\nApprove and send this reply? [y/N] ").strip().lower()
            approved = answer in ("y", "yes")
        else:
            approved = args.approve
        with run_context(args.ticket_id, config["configurable"]["thread_id"], step="decision"):
            state = await graph.ainvoke(
                Command(resume={"approved": approved, "reviewer": args.reviewer}), config, context=ctx
            )
        print(
            f"\n{state['status'].capitalize()} by {args.reviewer}. (On the fake backend nothing is really sent.)"
        )
    return 0


def main() -> int:
    load_dotenv()
    from copilot.privacy import install_log_redaction

    install_log_redaction()
    parser = argparse.ArgumentParser(
        prog="python -m copilot.agent",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tickets", help="list new tickets")
    r = sub.add_parser("run", help="run the agent on one ticket")
    r.add_argument("ticket_id", type=int)
    r.add_argument("--model", choices=["claude", "oracle"], default="claude")
    r.add_argument("--rag-models", action="store_true", help="dense search and reranker for policies")
    r.add_argument("--reviewer", default=os.environ.get("USER") or os.environ.get("USERNAME") or "staff")
    decision = r.add_mutually_exclusive_group()
    decision.add_argument(
        "--approve", dest="approve", action="store_true", default=None, help="approve without asking"
    )
    decision.add_argument("--reject", dest="approve", action="store_false", help="reject without asking")
    r.add_argument("--verbose", action="store_true", help="show every tool call")
    r.add_argument("--no-tracing", action="store_true", help="do not send traces to Phoenix")
    args = parser.parse_args()

    if args.command == "tickets":
        asyncio.run(list_tickets())
        return 0
    if not args.no_tracing:
        setup_tracing("agent-cli")
    try:
        return asyncio.run(run(args))
    finally:
        shutdown_tracing()


if __name__ == "__main__":
    sys.exit(main())
