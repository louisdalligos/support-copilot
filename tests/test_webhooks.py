"""The Zendesk webhook: signatures, freshness, duplicates and the background run."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from starlette.testclient import TestClient
from test_approvals import DUPLICATE, service

from copilot.approvals.web import create_app
from copilot.approvals.webhooks import MAX_BODY_BYTES, verify, zendesk_signature

SECRET = "dGhpc19zZWNyZXRfaXNfZm9yX3Rlc3Rpbmdfb25seQ=="


def stamp(at: datetime | None = None) -> str:
    return (at or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def signed(body: bytes, ts: str | None = None, secret: str = SECRET) -> dict[str, str]:
    ts = ts or stamp()
    return {
        "X-Zendesk-Webhook-Signature": zendesk_signature(secret, ts, body),
        "X-Zendesk-Webhook-Signature-Timestamp": ts,
        "Content-Type": "application/json",
    }


def test_signature_matches_zendesk_example():
    # HMAC-SHA256 over timestamp + body, base64-encoded, as Zendesk documents it.
    import base64
    import hashlib
    import hmac

    ts, body = "2026-10-20T01:02:03Z", b'{"ticket_id": "7"}'
    expected = base64.b64encode(
        hmac.new(SECRET.encode(), (ts + body.decode()).encode(), hashlib.sha256).digest()
    )
    assert zendesk_signature(SECRET, ts, body) == expected.decode()


def test_verify_refuses_wrong_missing_and_stale():
    body = b'{"ticket_id": 1}'
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    ts = stamp(now)
    good = zendesk_signature(SECRET, ts, body)
    assert verify(SECRET, ts, good, body, now)
    assert not verify(SECRET, ts, good, b'{"ticket_id": 2}', now)
    assert not verify("other", ts, good, body, now)
    assert not verify(SECRET, None, good, body, now)
    assert not verify(SECRET, ts, None, body, now)
    assert not verify(SECRET, ts, good, body, now + timedelta(minutes=6))
    assert verify(SECRET, ts, good, body, now + timedelta(minutes=4))


@pytest.fixture
def app_and_service(monkeypatch):
    monkeypatch.setenv("ZENDESK_WEBHOOK_SECRET", SECRET)
    svc = service()
    return create_app(svc, model_name="oracle"), svc


def wait_for_run(svc, ticket_id: int, seconds: float = 20) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if asyncio.run(svc.has_run(ticket_id)):
            return True
        time.sleep(0.05)
    return False


def test_new_ticket_is_drafted_in_the_background(app_and_service):
    app, svc = app_and_service
    body = json.dumps({"ticket_id": str(DUPLICATE)}).encode()  # Zendesk sends placeholders as strings
    with TestClient(app) as client:
        r = client.post("/webhooks/zendesk", content=body, headers=signed(body))
        assert r.status_code == 202 and r.json() == {"status": "queued", "ticket_id": DUPLICATE}
        assert wait_for_run(svc, DUPLICATE)
        # Zendesk retries; a second delivery does not start a second run.
        r = client.post("/webhooks/zendesk", content=body, headers=signed(body))
        assert r.status_code == 200 and r.json()["status"] == "already handled"
    records = asyncio.run(svc.records())
    assert [r["ticket_id"] for r in records] == [DUPLICATE]
    assert records[0]["status"] == "pending"  # waits for a person; nothing sent
    ticket = svc.backends.zendesk.get(DUPLICATE)
    assert [c["author"] for c in ticket["comments"] if c["public"]] == ["customer"]


def test_requests_without_a_valid_signature_are_refused(app_and_service):
    app, svc = app_and_service
    body = json.dumps({"ticket_id": DUPLICATE}).encode()
    with TestClient(app) as client:
        assert client.post("/webhooks/zendesk", content=body).status_code == 401
        bad = signed(body, secret="wrong")
        assert client.post("/webhooks/zendesk", content=body, headers=bad).status_code == 401
        old = signed(body, ts=stamp(datetime.now(UTC) - timedelta(hours=1)))
        assert client.post("/webhooks/zendesk", content=body, headers=old).status_code == 401
        # The signature replaces the same-origin check: a foreign Origin header does not matter.
        foreign = signed(b"{}") | {"Origin": "https://evil.example"}
        assert client.post("/webhooks/zendesk", content=b"{}", headers=foreign).status_code == 400
        big = b'{"ticket_id": 1, "pad": "' + b"x" * MAX_BODY_BYTES + b'"}'
        assert client.post("/webhooks/zendesk", content=big, headers=signed(big)).status_code == 413
    assert asyncio.run(svc.records()) == []


def test_webhook_is_off_without_a_secret(monkeypatch):
    monkeypatch.delenv("ZENDESK_WEBHOOK_SECRET", raising=False)
    body = b'{"ticket_id": 1}'
    with TestClient(create_app(service(), model_name="oracle")) as client:
        assert client.post("/webhooks/zendesk", content=body, headers=signed(body)).status_code == 404


def test_a_failed_run_can_be_retried(app_and_service):
    app, svc = app_and_service
    body = b'{"ticket_id": 99999}'  # no such ticket: the run fails
    with TestClient(app) as client:
        assert client.post("/webhooks/zendesk", content=body, headers=signed(body)).status_code == 202
        time.sleep(1)
        assert client.post("/webhooks/zendesk", content=body, headers=signed(body)).status_code == 202
