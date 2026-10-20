"""A real Zendesk account, through the Zendesk Support API.

LiveZendesk implements the same ZendeskBackend interface as FakeZendesk and returns
the same shapes, so the MCP server, the agent and the approval app do not change.

Sign-in uses OAuth with the client credentials grant: a confidential OAuth client's
identifier and secret are exchanged for a bearer token (30 minutes by default), which
is renewed before it expires or when Zendesk answers "invalid_token". Zendesk is
retiring API tokens (no new ones from 2026-10-27, all stop on 2027-04-30), and this is
the grant it recommends for server-to-server work.

Zendesk records everything done with the client's tokens as the user who created the
client, so the copilot's internal notes and the approved replies appear under that name.

Settings (environment variables):
    ZENDESK_SUBDOMAIN        the part before .zendesk.com
    ZENDESK_CLIENT_ID        Admin Center > Apps and integrations > APIs > OAuth clients
    ZENDESK_CLIENT_SECRET    (a Confidential client; the secret is shown once)
    ZENDESK_SCOPES           optional, default "read write"
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
    """Signs in with client credentials and sends API requests."""

    def __init__(
        self,
        subdomain: str,
        client_id: str,
        client_secret: str,
        *,
        scopes: str = "read write",
        http: httpx.Client | None = None,
    ) -> None:
        self.site = f"https://{subdomain_of(subdomain)}.zendesk.com"
        self.base = f"{self.site}/api/v2"
        self._http = http or httpx.Client(timeout=30)
        self._client = {"client_id": client_id.strip(), "client_secret": client_secret.strip()}
        self.scopes = scopes
        self._token: str | None = None
        self._expires = 0.0

    @classmethod
    def from_env(cls) -> ZendeskClient:
        keys = ("ZENDESK_SUBDOMAIN", "ZENDESK_CLIENT_ID", "ZENDESK_CLIENT_SECRET")
        missing = [k for k in keys if not os.environ.get(k)]
        if missing:
            raise SystemExit(f"Live Zendesk needs {', '.join(missing)} (see docs/zendesk.md).")
        return cls(
            os.environ["ZENDESK_SUBDOMAIN"],
            os.environ["ZENDESK_CLIENT_ID"],
            os.environ["ZENDESK_CLIENT_SECRET"],
            scopes=os.environ.get("ZENDESK_SCOPES") or "read write",
        )

    def token(self, *, renew: bool = False) -> str:
        """A bearer token, fetched again a minute before it expires (there is no refresh token)."""
        if renew or not self._token or time.monotonic() > self._expires - 60:
            r = self._http.post(
                f"{self.site}/oauth/tokens",
                data={"grant_type": "client_credentials", "scope": self.scopes, **self._client},
            )
            if r.status_code >= 400:
                raise ZendeskError(
                    f"Zendesk sign-in failed ({r.status_code}): {r.text[:300]}. "
                    "Check ZENDESK_SUBDOMAIN and the OAuth client's identifier, secret and kind (Confidential)."
                )
            body = r.json()
            self._token = body["access_token"]
            self._expires = time.monotonic() + float(body.get("expires_in") or 1800)
        return self._token  # type: ignore[return-value]

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        renewed = False
        for attempt in range(4):
            headers = {"Authorization": f"Bearer {self.token()}"}
            r = self._http.request(method, f"{self.base}{path}", headers=headers, **kwargs)
            if r.status_code == 401 and not renewed:  # expired or revoked: sign in again once
                self.token(renew=True)
                renewed = True
                continue
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
