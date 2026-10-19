"""The generated data must be reproducible, consistent and correctly labeled."""

from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from copilot.kb import load_kb
from copilot.policy import Problem, check_refund
from copilot.seed import build_dataset
from copilot.seed.build import check, render
from copilot.seed.evals import ACTIONS, INTENTS

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ds():
    return build_dataset()


@pytest.fixture(scope="module")
def kb_ids():
    return {d.id for d in load_kb(ROOT / "data" / "kb")}


def test_build_is_deterministic(ds):
    assert render(ds) == render(build_dataset())


def test_committed_files_are_up_to_date(ds):
    assert check(ds, ROOT / "data" / "generated") == []


def test_ids_are_unique(ds):
    for rows in (ds.products, ds.customers, ds.orders, ds.transactions, ds.refunds, ds.tickets):
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids))
    ticket_ids = [t["id"] for t in ds.tickets] + [e["ticket"]["id"] for e in ds.eval_tickets]
    assert len(ticket_ids) == len(set(ticket_ids))


def test_references_resolve(ds):
    customers = {c["id"] for c in ds.customers}
    orders = {o["id"] for o in ds.orders}
    products = {p["id"] for p in ds.products}
    assert all(o["customer_id"] in customers for o in ds.orders)
    assert all(li["product_id"] in products for o in ds.orders for li in o["line_items"])
    for rows in (ds.transactions, ds.refunds, ds.disputes):
        assert all(r["order_id"] in orders for r in rows)
    assert all(t["order_id"] in orders for t in ds.tickets if t["order_id"])


def test_order_totals_add_up(ds):
    for o in ds.orders:
        subtotal = sum(Decimal(li["price"]) * li["quantity"] for li in o["line_items"])
        assert Decimal(o["subtotal"]) == subtotal
        assert Decimal(o["total"]) == subtotal + Decimal(o["shipping"])


def test_refunds_never_exceed_payments(ds):
    for o in ds.orders:
        paid = sum(
            Decimal(t["amount"])
            for t in ds.transactions
            if t["order_id"] == o["id"] and t["kind"] == "sale" and t["status"] == "success"
        )
        refunded = sum(Decimal(r["amount"]) for r in ds.refunds if r["order_id"] == o["id"])
        assert refunded <= paid, o["name"]


def test_delivery_never_in_the_future(ds):
    for o in ds.orders:
        if o["delivered_at"]:
            assert o["shipped_at"] <= o["delivered_at"] <= "2026-10-15"


def test_all_people_are_synthetic(ds):
    assert all(c["email"].endswith("@example.com") for c in ds.customers)


def test_past_refund_status_tickets_match_real_refunds(ds):
    refunded_orders = {r["order_id"] for r in ds.refunds}
    for t in ds.tickets:
        if "refund_status" in t["tags"]:
            assert t["order_id"] in refunded_orders


def test_some_past_tickets_quote_the_old_policy(ds):
    assert any("kb:refund-policy-v1" in t["tags"] for t in ds.tickets)


def test_eval_labels_are_valid(ds, kb_ids):
    assert len(ds.eval_tickets) >= 50
    for e in ds.eval_tickets:
        labels = e["labels"]
        assert labels["intent"] in INTENTS
        assert labels["expected_action"] in ACTIONS
        assert set(labels["expected_docs"]) <= kb_ids, e["case_id"]
        assert (labels["refund_amount"] is not None) == (labels["expected_action"] == "propose_refund")


def test_injection_cases_never_expect_a_refund(ds):
    injections = [e for e in ds.eval_tickets if e["labels"]["injection"]]
    assert len(injections) >= 5
    assert all(e["labels"]["expected_action"] != "propose_refund" for e in injections)


def test_refund_labels_agree_with_the_policy_module(ds):
    """Re-derive every refund label from the order data and the policy rules."""
    orders = {o["name"]: o for o in ds.orders}
    checked = 0
    for e in ds.eval_tickets:
        labels = e["labels"]
        inputs = labels["refund_inputs"]
        if not inputs:
            continue
        order = orders[labels["order_name"]]
        item = order["line_items"][0]
        decision = check_refund(
            item_type=item["product_type"],
            delivered_on=date.fromisoformat(order["delivered_at"]) if order["delivered_at"] else None,
            opened=inputs["opened"],
            problem=Problem(inputs["problem"]),
            final_sale=item["final_sale"],
            shipped=order["fulfillment_status"] == "fulfilled",
        )
        assert decision.reason == labels["policy_reason"], e["case_id"]
        checked += 1
    assert checked >= 15


def test_splits_are_by_scenario_group(ds):
    groups: dict[str, set[str]] = {}
    for e in ds.eval_tickets:
        groups.setdefault(e["group"], set()).add(e["split"])
    assert all(len(splits) == 1 for splits in groups.values())
    counts = Counter(e["split"] for e in ds.eval_tickets)
    assert counts["dev"] > 0 and counts["test"] > 0
    assert any(e["labels"]["injection"] and e["split"] == "test" for e in ds.eval_tickets)


def test_rag_questions_reference_real_docs(ds, kb_ids):
    assert len(ds.rag_questions) >= 40
    for q in ds.rag_questions:
        assert set(q["relevant_doc_ids"]) <= kb_ids, q["id"]
    assert any(not q["answerable"] for q in ds.rag_questions)
    assert {q["split"] for q in ds.rag_questions} == {"dev", "test"}
