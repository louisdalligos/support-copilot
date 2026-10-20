"""The live Zendesk backend and the demo-ticket script, against a stand-in for Zendesk's API (no network)."""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from copilot.zendesk.backend import FakeZendesk, TicketNotFound, connect
from copilot.zendesk.demo_desk import DEMO_TAG, LIVE_TAG, DemoDesk, demo_tickets, shift
from copilot.zendesk.live import LiveZendesk, ZendeskClient, ZendeskError, subdomain_of
from copilot.zendesk.server import default_data_dir

CUSTOMER = {"id": 501, "name": "Ana Lee", "email": "ana.lee@example.com"}
AGENT = {"id": 900, "name": "Support Bot", "email": "agent@example.com"}


class FakeAPI:
    """Just enough of the Zendesk Support API for these tests."""

    def __init__(self) -> None:
        self.tickets: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.users: dict[int, dict[str, Any]] = {CUSTOMER["id"]: dict(CUSTOMER), AGENT["id"]: dict(AGENT)}
        self.calls: list[tuple[str, str, Any]] = []
        self.limit_next = 0
        self.next_id = 1

    def add(self, subject: str, body: str, requester: int = CUSTOMER["id"], **extra: Any) -> int:
        tid = self.next_id
        self.next_id += 1
        self.tickets[tid] = {
            "id": tid,
            "subject": subject,
            "description": body,
            "status": "new",
            "priority": None,
            "tags": [],
            "via": {"channel": "email"},
            "created_at": "2026-10-19T08:00:00Z",
            "requester_id": requester,
            "external_id": None,
            **extra,
        }
        self.comments[tid] = [
            {"author_id": requester, "public": True, "body": body, "created_at": "2026-10-19T08:00:00Z"}
        ]
        return tid

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path.removeprefix("/api/v2"), request.method
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body))
        assert request.headers["authorization"].startswith("Basic ")
        if self.limit_next:
            self.limit_next -= 1
            return httpx.Response(429, headers={"Retry-After": "0"})
        q = request.url.params

        if method == "GET" and path.startswith("/tickets/") and path.endswith("/comments.json"):
            tid = int(path.split("/")[2])
            page = int(q.get("page", "1"))
            items = self.comments[tid]
            chunk = items[(page - 1) * 2 : page * 2]
            nxt = f"https://acme.zendesk.com/api/v2{path}?page={page + 1}" if page * 2 < len(items) else None
            return httpx.Response(200, json={"comments": chunk, "users": [CUSTOMER], "next_page": nxt})
        if method == "GET" and path.startswith("/tickets/") and path.endswith(".json"):
            tid = int(path.split("/")[2].removesuffix(".json"))
            if tid not in self.tickets:
                return httpx.Response(404, json={"error": "RecordNotFound"})
            return httpx.Response(200, json={"ticket": self.tickets[tid], "users": [CUSTOMER]})
        if method == "GET" and path == "/tickets.json":
            found = [t for t in self.tickets.values() if t.get("external_id") == q.get("external_id")]
            return httpx.Response(200, json={"tickets": found})
        if method == "GET" and path == "/search.json":
            return self.search(q.get("query", ""), int(q.get("page", "1")))
        if method == "GET" and path.startswith("/users/"):
            if path == "/users/me.json":
                return httpx.Response(200, json={"user": AGENT})
            return httpx.Response(
                200, json={"user": self.users[int(path.split("/")[2].removesuffix(".json"))]}
            )
        if method == "POST" and path == "/users/create_or_update.json":
            u = body["user"]
            found = next((x for x in self.users.values() if x["email"] == u["email"]), None)
            if not found:
                found = {"id": 600 + len(self.users), "name": u["name"], "email": u["email"]}
                self.users[found["id"]] = found
            return httpx.Response(200, json={"user": found})
        if method == "POST" and path in ("/imports/tickets.json", "/tickets.json"):
            t = body["ticket"]
            first = t["comments"][0]["value"] if "comments" in t else t["comment"]["body"]
            tid = self.add(
                t["subject"], first, t["requester_id"], tags=t["tags"], external_id=t.get("external_id")
            )
            return httpx.Response(201, json={"ticket": self.tickets[tid]})
        if method == "PUT" and path.endswith("/tags.json"):
            tid = int(path.split("/")[2])
            if tid not in self.tickets:
                return httpx.Response(404)
            self.tickets[tid]["tags"] = sorted(set(self.tickets[tid]["tags"]) | set(body["tags"]))
            return httpx.Response(200, json={"tags": self.tickets[tid]["tags"]})
        if method == "PUT" and path.startswith("/tickets/"):
            tid = int(path.split("/")[2].removesuffix(".json"))
            if tid not in self.tickets:
                return httpx.Response(404)
            t = body["ticket"]
            if "comment" in t:
                self.comments[tid].append(
                    {"author_id": AGENT["id"], "created_at": "2026-10-19T09:00:00Z", **t["comment"]}
                )
            for key in ("priority", "status"):
                if key in t:
                    self.tickets[tid][key] = t[key]
            return httpx.Response(
                200, json={"ticket": self.tickets[tid], "audit": {"created_at": "2026-10-19T09:00:00Z"}}
            )
        return httpx.Response(500, json={"error": f"unexpected {method} {path}"})

    def search(self, query: str, page: int) -> httpx.Response:
        terms = query.split()
        assert terms[0] == "type:ticket"
        hits = list(self.tickets.values())
        for term in terms[1:]:
            if term.startswith("status:"):
                hits = [t for t in hits if t["status"] == term.split(":", 1)[1]]
            elif term.startswith("tags:"):
                hits = [t for t in hits if term.split(":", 1)[1] in t["tags"]]
            elif term.startswith("requester:"):
                email = term.split(":", 1)[1].strip('"')
                hits = [t for t in hits if self.users[t["requester_id"]]["email"] == email]
            else:
                hits = [t for t in hits if term.lower() in (t["subject"] + t["description"]).lower()]
        hits = sorted(hits, key=lambda t: t["id"], reverse=True)
        chunk = [{**t, "result_type": "ticket"} for t in hits[(page - 1) * 2 : page * 2]]
        more = httpx.QueryParams({"query": query, "page": page + 1})
        nxt = f"https://acme.zendesk.com/api/v2/search.json?{more}" if page * 2 < len(hits) else None
        return httpx.Response(200, json={"results": chunk, "users": [CUSTOMER], "next_page": nxt})


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


def client(api: FakeAPI) -> ZendeskClient:
    return ZendeskClient(
        "acme", "agent@example.com", "tok", http=httpx.Client(transport=httpx.MockTransport(api))
    )


def desk(api: FakeAPI) -> LiveZendesk:
    return LiveZendesk(client(api))


def test_subdomain_accepts_urls_and_hosts():
    assert subdomain_of("acme") == "acme"
    assert subdomain_of("acme.zendesk.com") == "acme"
    assert subdomain_of("https://acme.zendesk.com/agent/tickets/1") == "acme"


def test_auth_uses_api_token(api):
    c = client(api)
    assert c._auth == ("agent@example.com/token", "tok")


def test_get_returns_the_fake_shape_with_every_comment(api):
    tid = api.add("Charged twice", "Two charges for #1792")
    api.comments[tid] += [
        {"author_id": AGENT["id"], "public": False, "body": "note", "created_at": "2026-10-19T08:30:00Z"},
        {
            "author_id": CUSTOMER["id"],
            "public": True,
            "body": "any news?",
            "created_at": "2026-10-19T08:40:00Z",
        },
    ]
    t = desk(api).get(tid)
    fake = FakeZendesk.from_generated(default_data_dir()).get(10301)
    assert set(t) == set(fake)
    assert t["requester"] == {"name": "Ana Lee", "email": "ana.lee@example.com"}
    assert [(c["author"], c["public"]) for c in t["comments"]] == [
        ("customer", True),
        ("agent", False),
        ("customer", True),
    ]  # three comments over two pages
    assert t["priority"] == "normal" and t["channel"] == "email"


def test_missing_ticket_raises_ticket_not_found(api):
    with pytest.raises(TicketNotFound):
        desk(api).get(99)
    with pytest.raises(TicketNotFound):
        desk(api).add_tags(99, ["x"])


def test_search_filters_pages_and_uses_description(api):
    for i in range(5):
        api.add(f"Refund {i}", f"refund please {i}")
    api.tickets[2]["status"] = "solved"
    found = desk(api).search("refund", status="new", limit=10)
    assert [t["id"] for t in found] == [5, 4, 3, 1]
    assert found[0]["comments"][0]["body"] == "refund please 4"
    assert len(desk(api).search("", status="new", limit=2)) == 2


def test_by_requester(api):
    api.add("Hi", "hello")
    assert [t["id"] for t in desk(api).by_requester("Ana.Lee@Example.com")] == [1]


def test_writes(api):
    tid = api.add("Hi", "hello")
    z = desk(api)
    note = z.add_internal_note(tid, "draft ready", "copilot")
    assert note == {
        "author": "copilot",
        "public": False,
        "created_at": "2026-10-19T09:00:00Z",
        "body": "draft ready",
    }
    assert z.add_public_reply(tid, "Hello Ana", "sam")["public"] is True
    assert api.comments[tid][-1] | {"public": True} == api.comments[tid][-1]
    assert z.add_tags(tid, ["copilot"]) == ["copilot"]
    assert z.set_priority(tid, "high") == "high" and api.tickets[tid]["priority"] == "high"
    assert z.set_status(tid, "solved") == "solved"
    with pytest.raises(ValueError):
        z.set_status(tid, "bogus")


def test_rate_limit_is_retried(api):
    api.add("Hi", "hello")
    api.limit_next = 2
    assert desk(api).get(1)["id"] == 1


def test_errors_are_readable(api):
    api.add("Hi", "hello")
    api.limit_next = 9
    with pytest.raises(ZendeskError, match="rate-limiting"):
        desk(api).get(1)


def test_connect_picks_the_backend(monkeypatch):
    monkeypatch.delenv("COPILOT_ZENDESK", raising=False)
    assert isinstance(connect(default_data_dir()), FakeZendesk)
    monkeypatch.setenv("COPILOT_ZENDESK", "live")
    for key in ("ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit, match="ZENDESK_SUBDOMAIN"):
        connect(default_data_dir())
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESK_EMAIL", "a@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "t")
    assert isinstance(connect(default_data_dir()), LiveZendesk)


# The demo-ticket script


def test_shift_keeps_age_and_never_goes_past_now():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    assert shift("2026-10-10T07:11:00Z", 5, now) == "2026-10-15T07:11:00Z"
    assert shift("2026-10-15T13:00:00Z", 5, now) == "2026-10-20T12:00:00Z"


def test_setup_imports_once_with_customers_and_dates(api):
    d = DemoDesk(client(api), today=date(2026, 10, 20))
    made = d.setup(out=io.StringIO())
    tickets = demo_tickets()
    assert made == len(tickets) == len(api.tickets)
    imports = [c for c in api.calls if c[1] == "/imports/tickets.json"]
    first = imports[0][2]["ticket"]
    seed = tickets[0]
    assert first["external_id"] == f"northwind-{seed['id']}" and first["tags"] == [DEMO_TAG]
    assert first["comments"][0]["author_id"] == first["requester_id"]
    assert first["created_at"].startswith("2026-10-20")  # seed date 2026-10-15, five days on
    assert api.users[first["requester_id"]]["email"] == seed["requester_email"].lower()

    assert d.setup(out=io.StringIO()) == 0  # safe to run again
    out = io.StringIO()
    assert d.status(out=out) == len(tickets)


def test_send_creates_a_normal_ticket_from_the_customer(api):
    d = DemoDesk(client(api))
    ticket = d.send(10301)
    method, path, body = api.calls[-1]
    assert (method, path) == ("POST", "/tickets.json")
    assert body["ticket"]["comment"]["author_id"] == body["ticket"]["requester_id"]
    assert ticket["tags"] == [LIVE_TAG] and "#1792" in ticket["description"]
    with pytest.raises(SystemExit):
        d.send(1)
