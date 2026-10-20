"""The approval app's web pages (Starlette + Jinja2).

    GET  /                        the queue: waiting, escalated, decided
    GET  /tickets                 new tickets, with a button to run the copilot
    POST /tickets/{id}/run        run the copilot on a ticket
    GET  /runs/{thread_id}        review one run: ticket, evidence, flags, draft
    POST /runs/{thread_id}/decide approve (optionally with an edited reply) or reject
    GET  /healthz                 liveness, for Docker and uptime checks

Meant to run on localhost, or behind a reverse proxy that adds sign-in (Phase 4).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from jinja2 import DictLoader, Environment, select_autoescape
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from copilot.approvals.service import ApprovalError, ApprovalService
from copilot.approvals.templates import TEMPLATES

env = Environment(loader=DictLoader(TEMPLATES), autoescape=select_autoescape(default=True))


def _default_reviewer() -> str:
    return os.environ.get("COPILOT_REVIEWER") or os.environ.get("USER") or os.environ.get("USERNAME") or ""


def create_app(
    service: ApprovalService | None = None,
    *,
    open_service: Callable[[], Any] | None = None,
    storage_kind: str = "memory",
    model_name: str = "",
) -> Starlette:
    """Build the app around a ready service, or around `open_service`, an async context
    manager factory that yields one (used to open and close database connections)."""
    holder: dict[str, Any] = {"service": service, "storage": storage_kind}

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if open_service is None:
            yield
            return
        async with open_service() as (svc, kind):
            holder["service"], holder["storage"] = svc, kind
            yield

    def svc() -> ApprovalService:
        return holder["service"]

    def render(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        html = env.get_template(name).render(
            storage=holder["storage"],
            model=model_name,
            notice=request.query_params.get("notice"),
            notice_kind=request.query_params.get("kind", ""),
            reviewer=_default_reviewer(),
            **ctx,
        )
        return HTMLResponse(html, status_code=status)

    def go(path: str, notice: str | None = None, kind: str = "") -> RedirectResponse:
        if notice:
            path += f"?notice={quote(notice)}" + (f"&kind={kind}" if kind else "")
        return RedirectResponse(path, status_code=303)

    async def queue(request: Request) -> Response:
        records = await svc().records()
        return render(
            request,
            "queue.html",
            pending=[r for r in records if r["status"] == "pending"],
            escalated=[r for r in records if r["status"] == "escalated"],
            decided=[r for r in records if r["status"] in ("approved", "rejected")][:20],
        )

    async def tickets(request: Request) -> Response:
        new = svc().backends.zendesk.search("", status="new", limit=500)
        rows = [
            {"id": t["id"], "subject": t["subject"], "preview": t["comments"][0]["body"][:140]}
            for t in sorted(new, key=lambda t: t["id"])
        ]
        runs = {r["ticket_id"]: r["thread_id"] for r in await svc().records()}
        return render(request, "tickets.html", tickets=rows, runs=runs)

    async def run_ticket(request: Request) -> Response:
        ticket_id = int(request.path_params["ticket_id"])
        try:
            record = await svc().start(ticket_id)
        except ApprovalError as e:
            return go("/tickets", str(e), "error")
        note = "Escalated to the team." if record["status"] == "escalated" else "Draft ready for review."
        return go(f"/runs/{record['thread_id']}", note)

    async def review(request: Request) -> Response:
        try:
            info = await svc().detail(request.path_params["thread_id"])
        except ApprovalError as e:
            return render(
                request, "queue.html", status=404, pending=[], escalated=[], decided=[], notice=str(e)
            )
        return render(request, "run.html", **info)

    async def decide(request: Request) -> Response:
        thread_id = request.path_params["thread_id"]
        form = await request.form()
        decision = form.get("decision")
        reviewer = str(form.get("reviewer") or "").strip()
        if decision not in ("approve", "reject") or not reviewer:
            return go(f"/runs/{thread_id}", "Choose approve or reject, and give your name.", "error")
        try:
            result = await svc().decide(
                thread_id,
                approved=decision == "approve",
                reviewer=reviewer,
                reply=str(form.get("reply") or ""),
            )
        except ApprovalError as e:
            return go(f"/runs/{thread_id}", str(e), "error")
        if decision == "approve":
            refund = result.get("refund")
            note = "Reply sent." + (f" Refund of ${refund['amount']} issued." if refund else "")
        else:
            note = "Rejected. The ticket is back in the team's queue."
        return go("/", note)

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": svc() is not None, "storage": holder["storage"]})

    routes = [
        Route("/", queue),
        Route("/tickets", tickets),
        Route("/tickets/{ticket_id:int}/run", run_ticket, methods=["POST"]),
        Route("/runs/{thread_id}", review),
        Route("/runs/{thread_id}/decide", decide, methods=["POST"]),
        Route("/healthz", healthz),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
