"""Zendesk webhook: run the copilot on a new ticket as soon as it arrives.

A Zendesk trigger ("ticket is created") calls POST /webhooks/zendesk with the body
{"ticket_id": "{{ticket.id}}"}. The endpoint:

1. Checks Zendesk's signature: base64(HMAC-SHA256(secret, timestamp + body)), in the
   X-Zendesk-Webhook-Signature and X-Zendesk-Webhook-Signature-Timestamp headers.
   Requests older than five minutes are refused, so a captured request cannot be replayed later.
2. Ignores a ticket that already has a run, so Zendesk's retries never create a second draft.
3. Answers 202 right away and runs the copilot in the background, one ticket at a time
   (Zendesk gives up on a webhook after a few seconds; a run takes longer).

The draft lands in the approval queue and as an internal note on the ticket. Nothing is
sent to the customer until a person approves it.

ZENDESK_WEBHOOK_SECRET is the webhook's signing secret (Admin Center > Webhooks > the
webhook > Reveal secret). Without it the endpoint answers 404.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from copilot.approvals.service import ApprovalError

log = logging.getLogger("copilot.webhooks")

MAX_AGE_SECONDS = 300
MAX_BODY_BYTES = 64 * 1024
SIGNATURE = "x-zendesk-webhook-signature"
TIMESTAMP = "x-zendesk-webhook-signature-timestamp"


def zendesk_signature(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify(
    secret: str, timestamp: str | None, signature: str | None, body: bytes, now: datetime | None = None
) -> bool:
    if not (secret and timestamp and signature):
        return False
    if not hmac.compare_digest(zendesk_signature(secret, timestamp, body), signature):
        return False
    try:
        sent = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=UTC)
    return abs(((now or datetime.now(UTC)) - sent).total_seconds()) <= MAX_AGE_SECONDS


class ZendeskWebhook:
    """The endpoint and its background worker. `start` runs the copilot on one ticket;
    `has_run` says whether a ticket already has a run."""

    def __init__(
        self,
        start: Callable[[int], Awaitable[Any]],
        has_run: Callable[[int], Awaitable[bool]],
        secret: str | None = None,
    ) -> None:
        self.start = start
        self.has_run = has_run
        self.secret = secret if secret is not None else os.environ.get("ZENDESK_WEBHOOK_SECRET", "")
        self.queued: set[int] = set()
        self.tasks: set[asyncio.Task[None]] = set()
        self.lock = asyncio.Lock()

    async def handle(self, request: Request) -> Response:
        if not self.secret:
            return Response(status_code=404)
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse({"error": "body too large"}, status_code=413)
        if not verify(self.secret, request.headers.get(TIMESTAMP), request.headers.get(SIGNATURE), body):
            return JSONResponse({"error": "invalid or expired signature"}, status_code=401)
        try:
            ticket_id = int(json.loads(body)["ticket_id"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": 'expected {"ticket_id": ...}'}, status_code=400)
        if ticket_id in self.queued or await self.has_run(ticket_id):
            return JSONResponse({"status": "already handled", "ticket_id": ticket_id})
        self.queued.add(ticket_id)
        task = asyncio.create_task(self._run(ticket_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return JSONResponse({"status": "queued", "ticket_id": ticket_id}, status_code=202)

    async def _run(self, ticket_id: int) -> None:
        async with self.lock:  # one run at a time: predictable load and API usage
            try:
                await self.start(ticket_id)
                log.info("webhook: drafted ticket %s", ticket_id)
            except ApprovalError as e:  # the agent could not run (e.g. the ticket is gone)
                log.warning("webhook: no draft for ticket %s: %s", ticket_id, e)
                self.queued.discard(ticket_id)
            except Exception:  # noqa: BLE001 - a failed run must not kill the worker
                log.exception("webhook: the copilot failed on ticket %s", ticket_id)
                self.queued.discard(ticket_id)  # let a later delivery try again
