"""A real Zendesk account, through the Zendesk Support API.

LiveZendesk implements the same ZendeskBackend interface as FakeZendesk and returns
the same shapes, so the MCP server, the agent and the approval app do not change.

Sign-in uses an API token: HTTP basic auth with "{agent email}/token" and the token.
The agent's writes (internal notes, tags, priority) and the approval app's public
replies are made as that agent.

Settings (environment variables):
    ZENDESK_SUBDOMAIN     the part before .zendesk.com
    ZENDESK_EMAIL         the agent the API token belongs to
    ZENDESK_API_TOKEN     Admin Center > Apps and integrations > Zendesk API
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from copilot.zendesk.backend import STATUSES, TicketNotFound


class ZendeskError(RuntimeError):
    """Zendesk refused a request; the message says why."""


def _ts(value: str | None) -> str:
    if not value:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def subdomain_of(value: str) -> str:
    """'acme', 'acme.zendesk.com' and 'https://acme.zendesk.com/agent' all mean 'acme'."""
    v = value.strip().removeprefix("https://").removeprefix("http://").split("/")[0]
    return v.removesuffix(".zendesk.com")


class ZendeskClient:
    def __init__(self, subdomain: str, email: str, token: str, *, http: httpx.Client | None = None) -> None:
        self.base = f"https://{subdomain_of(subdomain)}.zendesk.com/api/v2"
        self._http = http or httpx.Client(timeout=30)
        self._auth = (f"{email.strip()}/token", token.strip())

    @classmethod
    def from_env(cls) -> ZendeskClient:
        missing = [
            k for k in ("ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN") if not os.environ.get(k)
        ]
        if missing:
            raise SystemExit(f"Live Zendesk needs {', '.join(missing)} (see docs/zendesk.md).")
        return cls(
            os.environ["ZENDESK_SUBDOMAIN"], os.environ["ZENDESK_EMAIL"], os.environ["ZENDESK_API_TOKEN"]
        )

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(4):
            r = self._http.request(method, f"{self.base}{path}", auth=self._auth, **kwargs)
            if r.status_code == 429:
                if attempt == 3:
                    break
                time.sleep(min(float(r.headers.get("Retry-After", "2")), 30))
                continue
            if r.status_code == 404:
                raise LookupError(path)
            if r.status_code >= 400:
                raise ZendeskError(f"Zendesk API error ({r.status_code}) on {method} {path}: {r.text[:300]}")
            return r.json() if r.content else {}
        raise ZendeskError("Zendesk kept rate-limiting the request")


class LiveZendesk:
    """ZendeskBackend for a real Zendesk account. See the module docstring."""

    def __init__(self, client: ZendeskClient) -> None:
        self.client = client
        self._users: dict[int, dict[str, Any]] = {}

    @classmethod
    def from_env(cls) -> LiveZendesk:
        return cls(ZendeskClient.from_env())

    def _remember(self, users: list[dict[str, Any]]) -> None:
        for u in users:
            self._users[u["id"]] = u

    def _user(self, user_id: int | None) -> dict[str, Any]:
        if user_id is None:
            return {}
        if user_id not in self._users:
            self._remember([self.client.request("GET", f"/users/{user_id}.json")["user"]])
        return self._users[user_id]

    def _ticket(self, t: dict[str, Any], comments: list[dict[str, Any]] | None) -> dict[str, Any]:
        requester = self._user(t.get("requester_id"))
        if comments is None:  # search results: the description stands in for the first comment
            comments = [
                {
                    "author": "customer",
                    "public": True,
                    "created_at": _ts(t.get("created_at")),
                    "body": t.get("description") or "",
                }
            ]
        return {
            "id": t["id"],
            "subject": t.get("subject") or "",
            "status": t.get("status") or "new",
            "priority": t.get("priority") or "normal",
            "tags": list(t.get("tags") or []),
            "channel": (t.get("via") or {}).get("channel", "api"),
            "created_at": _ts(t.get("created_at")),
            "order_id": None,
            "requester": {"name": requester.get("name") or "", "email": requester.get("email") or ""},
            "comments": comments,
        }

    def _comments(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        out, url = [], f"/tickets/{ticket['id']}/comments.json?include=users"
        while url:
            page = self.client.request("GET", url)
            self._remember(page.get("users") or [])
            for c in page["comments"]:
                out.append(
                    {
                        "author": "customer" if c.get("author_id") == ticket.get("requester_id") else "agent",
                        "public": bool(c.get("public")),
                        "created_at": _ts(c.get("created_at")),
                        "body": c.get("body") or c.get("plain_body") or "",
                    }
                )
            nxt = page.get("next_page")
            url = nxt.split("/api/v2", 1)[1] if nxt else ""
        return out

    def get(self, ticket_id: int) -> dict[str, Any]:
        try:
            body = self.client.request("GET", f"/tickets/{int(ticket_id)}.json?include=users")
        except LookupError:
            raise TicketNotFound(ticket_id) from None
        self._remember(body.get("users") or [])
        t = body["ticket"]
        return self._ticket(t, self._comments(t))

    def _search(self, query: str, limit: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        params: dict[str, Any] | None = {
            "query": f"type:ticket {query}".strip(),
            "sort_by": "created_at",
            "sort_order": "desc",
            "include": "tickets(users)",
        }
        url = "/search.json"
        while url and len(out) < limit:
            page = self.client.request("GET", url, params=params)
            self._remember(page.get("users") or [])
            out += [
                self._ticket(t, None) for t in page.get("results") or [] if t.get("result_type") == "ticket"
            ]
            nxt = page.get("next_page")
            url, params = (nxt.split("/api/v2", 1)[1], None) if nxt else ("", None)
        return out[:limit]

    def search(
        self, query: str, *, status: str | None = None, tag: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        parts = [query.strip()]
        if status:
            parts.append(f"status:{status}")
        if tag:
            parts.append(f"tags:{tag}")
        return self._search(" ".join(p for p in parts if p), limit)

    def by_requester(self, email: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return self._search(f'requester:"{email.strip().lower()}"', limit)

    def _update(self, ticket_id: int, ticket: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.client.request("PUT", f"/tickets/{int(ticket_id)}.json", json={"ticket": ticket})
        except LookupError:
            raise TicketNotFound(ticket_id) from None

    def _comment(self, ticket_id: int, body: str, public: bool, author: str) -> dict[str, Any]:
        result = self._update(ticket_id, {"comment": {"body": body, "public": public}})
        audit = result.get("audit") or {}
        return {"author": author, "public": public, "created_at": _ts(audit.get("created_at")), "body": body}

    def add_internal_note(self, ticket_id: int, body: str, author: str) -> dict[str, Any]:
        return self._comment(ticket_id, body, False, author)

    def add_public_reply(self, ticket_id: int, body: str, author: str) -> dict[str, Any]:
        return self._comment(ticket_id, body, True, author)

    def add_tags(self, ticket_id: int, tags: list[str]) -> list[str]:
        try:
            result = self.client.request("PUT", f"/tickets/{int(ticket_id)}/tags.json", json={"tags": tags})
        except LookupError:
            raise TicketNotFound(ticket_id) from None
        return list(result.get("tags") or [])

    def set_priority(self, ticket_id: int, priority: str) -> str:
        self._update(ticket_id, {"priority": priority})
        return priority

    def set_status(self, ticket_id: int, status: str) -> str:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        self._update(ticket_id, {"status": status})
        return status
