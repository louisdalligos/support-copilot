"""Chargeback evidence: the packet, its PDF, and the approval app's chargeback pages."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_approvals import service

from copilot.approvals.service import ApprovalError
from copilot.approvals.web import create_app
from copilot.disputes import pdf
from copilot.disputes.evidence import DisputeNotFound, build_packet, open_disputes, policy_section
from copilot.shopify.backend import FakeShopify
from copilot.zendesk.backend import FakeZendesk

DATA = Path(__file__).resolve().parents[1] / "data" / "generated"
CASES = [json.loads(line) for line in (DATA / "dispute_cases.jsonl").read_text().splitlines() if line]
BY_GROUP = {c["group"]: c for c in CASES}
# Every input field of Shopify's ShopifyPaymentsDisputeEvidenceUpdateInput (Admin API 2026-07).
SHOPIFY_FIELDS = {
    "accessActivityLog", "cancellationPolicyDisclosure", "cancellationPolicyFile", "cancellationRebuttal",
    "customerCommunicationFile", "customerEmailAddress", "customerFirstName", "customerLastName",
    "refundPolicyDisclosure", "refundPolicyFile", "refundRefusalExplanation", "serviceDocumentationFile",
    "shippingAddress", "shippingDocumentationFile", "submitEvidence", "uncategorizedFile", "uncategorizedText",
}  # fmt: skip


def backends() -> tuple[FakeShopify, FakeZendesk]:
    return FakeShopify.from_generated(DATA), FakeZendesk.from_generated(DATA)


def packet(group: str) -> dict:
    shopify, zendesk = backends()
    return build_packet(shopify, zendesk, BY_GROUP[group]["dispute_id"])


def test_every_case_gets_the_labeled_recommendation_and_evidence():
    assert len(CASES) >= 4
    for case in CASES:
        p = packet(case["group"])
        assert p["recommendation"]["action"] == case["expected_recommendation"], case["group"]
        assert set(case["expected_evidence"]) <= set(p["evidence"]), case["group"]
        assert p["recommendation"]["why"]
        assert set(p["shopify_fields"]) <= SHOPIFY_FIELDS
        assert p["shopify_fields"]["submitEvidence"] is False  # a person submits


def test_not_received_but_delivered():
    p = packet("dispute-delivered-and-confirmed")
    o = p["order"]
    assert o["delivered"] and "marked it delivered on" in p["rebuttal"]
    assert "wrote to our support team about the order after it arrived" in p["rebuttal"]
    msgs = p["evidence"]["customer_communication"]["messages"]
    assert msgs and all(m["date"] >= o["delivered"] for m in msgs)
    dates = [e["date"] for e in p["timeline"]]
    assert dates == sorted(dates) and p["timeline"][-1]["event"].startswith("Chargeback opened")


def test_fraud_claim_uses_the_account_history():
    p = packet("dispute-fraud-returning-customer")
    rows = p["evidence"]["prior_undisputed_orders"]["rows"]
    assert len(rows) == 4
    assert "4 earlier orders" in p["rebuttal"] and "none were disputed" in p["rebuttal"]


def test_already_refunded_is_a_credit_issued_response():
    p = packet("dispute-already-refunded")
    assert next(iter(p["evidence"])) == "refund_record"  # the strongest evidence comes first
    assert "before this dispute was opened" in p["rebuttal"]
    assert p["shopify_fields"]["refundRefusalExplanation"].endswith("before the dispute was opened.")
    assert p["policy"]["title"] == "Refund policy: Damaged, defective or wrong items"
    assert p["shopify_fields"]["refundPolicyDisclosure"] == p["policy"]["text"]


def test_no_proof_of_delivery_means_accept():
    p = packet("dispute-never-delivered")
    assert p["recommendation"]["action"] == "accept" and p["rebuttal"] == ""
    assert "shipping_documentation" not in p["evidence"]


def test_open_disputes_soonest_first_and_unknown_dispute():
    shopify, zendesk = backends()
    found = open_disputes(shopify)
    assert [d["days_left"] for d in found] == sorted(d["days_left"] for d in found)
    assert {d["id"] for d in found} >= {c["dispute_id"] for c in CASES}
    with pytest.raises(DisputeNotFound):
        build_packet(shopify, zendesk, 999999)


def test_policy_excerpt_is_one_section():
    section = policy_section("refund-policy", "Damaged, defective or wrong items")
    assert section and "within 14 days of delivery" in section["text"]
    assert "Gift cards" not in section["text"]  # another section; issuers want only what is relevant
    assert policy_section("refund-policy", "No such heading") is None


def test_pdf_is_within_card_network_limits():
    for case in CASES:
        data = pdf.render(packet(case["group"]))
        assert data.startswith(b"%PDF") and len(data) < pdf.MAX_BYTES


def test_pdf_over_the_page_limit_is_refused(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PAGES", 1)
    with pytest.raises(pdf.EvidenceTooLarge, match="pages"):
        pdf.render(packet("dispute-delivered-and-confirmed"))


# The approval app


def test_chargeback_pages_and_decision():
    svc = service()
    client = TestClient(create_app(svc, model_name="oracle"))
    delivered = BY_GROUP["dispute-delivered-and-confirmed"]["dispute_id"]

    queue = client.get("/").text
    assert "need a response" in queue and 'href="/disputes"' in queue
    listing = client.get("/disputes").text
    assert all(f"#{c['dispute_id']}" in listing for c in CASES)

    page = client.get(f"/disputes/{delivered}").text
    assert (
        "Recommendation" in page
        and "marked it delivered on" in page
        and "&#34;submitEvidence&#34;: false" in page
    )

    got = client.get(f"/disputes/{delivered}/evidence.pdf")
    assert got.status_code == 200 and got.headers["content-type"] == "application/pdf"
    assert got.headers["cache-control"] == "no-store" and got.content.startswith(b"%PDF")

    done = client.post(f"/disputes/{delivered}/decide", data={"decision": "submitted", "reviewer": "x"})
    assert done.status_code == 200 and "Marked as submitted" in done.text
    decided = asyncio.run(svc.dispute_decisions())[delivered]
    assert decided | {"decision": "submitted", "recommended": "respond"} == decided
    assert client.get(f"/disputes/{delivered}").text.count("Marked submitted by") == 1

    again = client.post(f"/disputes/{delivered}/decide", data={"decision": "accepted", "reviewer": "x"})
    assert "already marked submitted" in again.text
    assert client.get("/disputes/999999").status_code == 404


def test_decide_dispute_checks_its_input():
    svc = service()
    with pytest.raises(ApprovalError):
        asyncio.run(svc.decide_dispute(CASES[0]["dispute_id"], decision="ignore", reviewer="x"))
    with pytest.raises(ApprovalError):
        asyncio.run(svc.decide_dispute(4001, decision="accepted", reviewer="x"))  # a closed, lost dispute
