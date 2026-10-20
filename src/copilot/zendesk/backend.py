"""Zendesk backends.

The MCP server talks to a ZendeskBackend. FakeZendesk serves the seed data in
memory, so the server runs and is tested without a Zendesk account. LiveZendesk
(copilot.zendesk.live) talks to a real account and returns the same shapes, so the
server does not change. connect() picks one: the real account when
COPILOT_ZENDESK=live, the seed data otherwise.

Ticket shapes follow the Zendesk Tickets API loosely: a ticket has a subject,
status, priority, tags, a requester and a list of comments. Internal notes are
comments with public=False.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

STATUSES = ("new", "open", "pending", "solved", "closed")
PRIORITIES = ("low", "normal", "high", "urgent")


class TicketNotFound(LookupError):
    def __init__(self, ticket_id: int) -> None:
        super().__init__(f"Ticket {ticket_id} not found")
        self.ticket_id = ticket_id


class ZendeskBackend(Protocol):
    def search(
        self, query: str, *, status: str | None, tag: str | None, limit: int
    ) -> list[dict[str, Any]]: ...
    def get(self, ticket_id: int) -> dict[str, Any]: ...
    def by_requester(self, email: str, *, limit: int) -> list[dict[str, Any]]: ...
    def add_internal_note(self, ticket_id: int, body: str, author: str) -> dict[str, Any]: ...
    def add_tags(self, ticket_id: int, tags: list[str]) -> list[str]: ...
    def set_priority(self, ticket_id: int, priority: str) -> str: ...
    # Used only by the approval app, after a person approves. Not exposed through MCP.
    def add_public_reply(self, ticket_id: int, body: str, author: str) -> dict[str, Any]: ...
    def set_status(self, ticket_id: int, status: str) -> str: ...


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw["id"],
        "subject": raw["subject"],
        "status": raw["status"],
        "priority": raw.get("priority", "normal"),
        "tags": list(raw.get("tags", [])),
        "channel": raw.get("channel", "email"),
        "created_at": raw["created_at"],
        "order_id": raw.get("order_id"),
        "requester": {"name": raw["requester_name"], "email": raw["requester_email"]},
        "comments": [dict(c) for c in raw["comments"]],
    }


class FakeZendesk:
    """An in-memory Zendesk. Writes last for the life of the process only."""

    def __init__(self, tickets: list[dict[str, Any]]) -> None:
        self._tickets = {t["id"]: _normalize(t) for t in tickets}

    @classmethod
    def from_generated(cls, data_dir: Path) -> FakeZendesk:
        """Load past tickets and the eval tickets from data/generated."""

        def rows(name: str) -> list[dict[str, Any]]:
            path = data_dir / name
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

        tickets = rows("tickets.jsonl") + [e["ticket"] for e in rows("eval_tickets.jsonl")]
        return cls(tickets)

    def _ticket(self, ticket_id: int) -> dict[str, Any]:
        try:
            return self._tickets[ticket_id]
        except KeyError:
            raise TicketNotFound(ticket_id) from None

    def search(
        self, query: str, *, status: str | None = None, tag: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        terms = query.lower().split()
        hits = []
        for t in self._tickets.values():
            if status and t["status"] != status:
                continue
            if tag and tag not in t["tags"]:
                continue
            text = " ".join(
                [t["subject"], t["requester"]["email"], *(c["body"] for c in t["comments"])]
            ).lower()
            if all(term in text for term in terms):
                hits.append(t)
        hits.sort(key=lambda t: t["created_at"], reverse=True)
        return [copy.deepcopy(t) for t in hits[:limit]]

    def get(self, ticket_id: int) -> dict[str, Any]:
        return copy.deepcopy(self._ticket(ticket_id))

    def by_requester(self, email: str, *, limit: int = 20) -> list[dict[str, Any]]:
        email = email.strip().lower()
        hits = [t for t in self._tickets.values() if t["requester"]["email"].lower() == email]
        hits.sort(key=lambda t: t["created_at"], reverse=True)
        return [copy.deepcopy(t) for t in hits[:limit]]

    def add_internal_note(self, ticket_id: int, body: str, author: str) -> dict[str, Any]:
        ticket = self._ticket(ticket_id)
        note = {"author": author, "public": False, "created_at": _now(), "body": body}
        ticket["comments"].append(note)
        return copy.deepcopy(note)

    def add_tags(self, ticket_id: int, tags: list[str]) -> list[str]:
        ticket = self._ticket(ticket_id)
        for tag in tags:
            if tag not in ticket["tags"]:
                ticket["tags"].append(tag)
        return list(ticket["tags"])

    def set_priority(self, ticket_id: int, priority: str) -> str:
        ticket = self._ticket(ticket_id)
        ticket["priority"] = priority
        return priority

    def add_public_reply(self, ticket_id: int, body: str, author: str) -> dict[str, Any]:
        ticket = self._ticket(ticket_id)
        reply = {"author": author, "public": True, "created_at": _now(), "body": body}
        ticket["comments"].append(reply)
        return copy.deepcopy(reply)

    def set_status(self, ticket_id: int, status: str) -> str:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        self._ticket(ticket_id)["status"] = status
        return status


def connect(data_dir: Path) -> ZendeskBackend:
    """The real Zendesk account when COPILOT_ZENDESK=live, otherwise the seed data in data_dir."""
    if os.environ.get("COPILOT_ZENDESK") == "live":
        from copilot.zendesk.live import LiveZendesk

        return LiveZendesk.from_env()
    return FakeZendesk.from_generated(data_dir)
