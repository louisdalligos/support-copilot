"""The knowledge base must parse, and its prose must agree with copilot.store."""

from pathlib import Path

import pytest

from copilot.kb import KbFormatError, load_kb, parse_doc
from copilot.store import (
    DAMAGE_REPORT_DAYS,
    DESCRIPTOR_ORDER,
    DESCRIPTOR_SUBSCRIPTION,
    EXPRESS_SHIPPING,
    FREE_SHIPPING_THRESHOLD,
    INTERNATIONAL_SHIPPING,
    REFUND_WINDOW_DAYS,
    STANDARD_SHIPPING,
    V1_FREE_SHIPPING_THRESHOLD,
    V1_REFUND_WINDOW_DAYS,
)

KB_DIR = Path(__file__).resolve().parents[1] / "data" / "kb"


@pytest.fixture(scope="module")
def docs():
    return {d.id: d for d in load_kb(KB_DIR)}


def test_corpus_size(docs):
    assert 30 <= len(docs) <= 50


def test_superseded_docs_point_to_current_docs(docs):
    superseded = [d for d in docs.values() if not d.is_current]
    assert superseded, "the corpus needs outdated versions to test stale-document handling"
    for doc in superseded:
        assert doc.superseded_by in docs
        assert docs[doc.superseded_by].is_current


def test_file_names_match_ids(docs):
    for doc in docs.values():
        assert doc.path.stem == doc.id


@pytest.mark.parametrize(
    ("doc_id", "fact"),
    [
        ("refund-policy", f"{REFUND_WINDOW_DAYS} days of delivery"),
        ("refund-policy", f"{DAMAGE_REPORT_DAYS} days of delivery"),
        ("refund-policy-v1", f"{V1_REFUND_WINDOW_DAYS} days of delivery"),
        ("shipping-policy", f"${STANDARD_SHIPPING}"),
        ("shipping-policy", f"${FREE_SHIPPING_THRESHOLD:.0f} or more"),
        ("shipping-policy", f"${EXPRESS_SHIPPING}"),
        ("shipping-policy-v1", f"${V1_FREE_SHIPPING_THRESHOLD:.0f} or more"),
        ("international-shipping", f"${INTERNATIONAL_SHIPPING}"),
        ("payment-methods", DESCRIPTOR_ORDER),
        ("payment-methods", DESCRIPTOR_SUBSCRIPTION),
        ("statement-descriptor", DESCRIPTOR_SUBSCRIPTION),
        ("refund-timelines", "5 to 10 business days"),
        ("pending-and-duplicate-charges", "3 to 7 business days"),
    ],
)
def test_docs_state_the_store_facts(docs, doc_id, fact):
    assert fact in docs[doc_id].body


def test_missing_front_matter_is_rejected(tmp_path):
    path = tmp_path / "bad.md"
    with pytest.raises(KbFormatError):
        parse_doc("# No front matter\n", path)


def test_superseded_without_pointer_is_rejected(tmp_path):
    text = (
        "---\nid: x\ntitle: X\ntype: policy\nversion: 1\nstatus: superseded\n"
        "effective_date: 2025-01-01\nurl: https://example.com\n---\nBody\n"
    )
    with pytest.raises(KbFormatError):
        parse_doc(text, tmp_path / "x.md")
