"""Retrieval: chunking, BM25, policy versions, fusion, reranking, the eval gate and the MCP server.

Models are replaced by small fakes here, so these tests need no downloads.
The real models are measured by `python -m copilot.rag.evaluate --dense --rerank`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import mcp_session
import pytest
from mcp.client.session import ClientSession
from mcp_session import error_text, payload

from copilot.kb import load_kb
from copilot.rag.chunks import chunk_docs
from copilot.rag.evaluate import evaluate, load_questions
from copilot.rag.lexical import stem, tokenize
from copilot.rag.retriever import Retriever
from copilot.rag.server import build_server

KB_DIR = Path(__file__).resolve().parents[1] / "data" / "kb"


@pytest.fixture(scope="module")
def docs():
    return {d.id: d for d in load_kb(KB_DIR)}


@pytest.fixture(scope="module")
def bm25():
    return Retriever(load_kb(KB_DIR))


class FakeEmbedder:
    """Maps one query to one chunk, standing in for a model that understands synonyms."""

    name = "fake-embedder"

    def __init__(self, query: str, target_chunk: str) -> None:
        self.query, self.target = query, target_chunk
        self.ids: list[str] = []

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts = list(texts)
        return [[1.0 if i == j else 0.0 for j in range(len(texts))] for i in range(len(texts))]

    def embed_query(self, text: str) -> list[float]:
        hit = next(i for i, t in enumerate(self.texts) if t.startswith(self.target))
        return [1.0 if i == hit and text == self.query else 0.0 for i in range(len(self.texts))]


class FakeReranker:
    """Scores 0.9 for chunks containing a keyword, 0.01 otherwise."""

    name = "fake-reranker"

    def __init__(self, keyword: str) -> None:
        self.keyword = keyword

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        return [0.9 if self.keyword in t.lower() else 0.01 for t in texts]


def test_chunks_carry_titles_and_versions(docs):
    chunks = chunk_docs(docs)
    assert {c.doc_id for c in chunks} == set(docs)
    refund = [c for c in chunks if c.doc_id == "refund-policy"]
    assert any(c.section == "Change of mind" for c in refund)
    assert all(c.text.startswith("Refund policy") for c in refund)
    assert len({c.id for c in chunks}) == len(chunks)
    old = [c for c in chunks if c.doc_id == "refund-policy-v1"]
    assert old and all(c.superseded_by == "refund-policy" for c in old)


def test_tokenizer_matches_word_forms():
    assert stem("refunds") == stem("refund") == "refund"
    assert stem("charged") == "charg" == stem("charging")
    assert stem("glass") == "glass"
    assert "the" not in tokenize("What is the refund window?")


def test_current_policy_by_default(bm25):
    result = bm25.search("how many days to return unopened coffee", k=10)
    assert result.doc_ids[0] == "refund-policy"
    assert "refund-policy-v1" not in result.doc_ids


def test_as_of_selects_the_version_in_force(bm25):
    old = bm25.search("how many days to return unopened coffee", as_of=date(2025, 12, 1))
    assert "refund-policy-v1" in old.doc_ids
    assert "refund-policy" not in old.doc_ids
    assert any("superseded" in n for n in old.notes)
    new = bm25.search("how many days to return unopened coffee", as_of=date(2026, 3, 1))
    assert "refund-policy" in new.doc_ids and "refund-policy-v1" not in new.doc_ids
    # Unversioned documents are unaffected by as_of.
    assert bm25.search("gift cards expire", as_of=date(2025, 6, 1)).doc_ids[0] == "gift-cards"


def test_include_superseded_returns_both_versions(bm25):
    result = bm25.search("refund policy days of delivery", k=10, include_superseded=True)
    assert {"refund-policy", "refund-policy-v1"} <= set(result.doc_ids)


def test_dense_results_are_fused_in(docs):
    query = "Can I put beans in the freezer?"  # BM25 misses this one: no shared words
    plain = Retriever(docs).search(query)
    assert "storing-coffee" not in plain.doc_ids
    hybrid = Retriever(docs, embedder=FakeEmbedder(query, "Storing coffee")).search(query)
    assert "storing-coffee" in hybrid.doc_ids
    assert hybrid.pipeline == "bm25 + dense(fake-embedder)"


def test_reranker_reorders_and_can_abstain(docs):
    reranked = Retriever(docs, reranker=FakeReranker("express"), threshold=0.2).search("shipping cost")
    assert reranked.relevant is True
    assert "express" in reranked.hits[0].chunk.text.lower()
    nothing = Retriever(docs, reranker=FakeReranker("matcha"), threshold=0.2).search("Do you sell matcha?")
    assert nothing.relevant is False
    unjudged = Retriever(docs, reranker=FakeReranker("express")).search("shipping cost")
    assert unjudged.relevant is None and unjudged.top_score == 0.9  # no cutoff by default


def test_bm25_eval_gate(bm25):
    report = evaluate(bm25, load_questions())
    assert report.stale == 0
    assert report.hit_at_5 >= 0.85, report.misses
    assert report.abstain_accuracy is None  # BM25 has no calibrated score to abstain with


def run(test):
    return mcp_session.run(
        lambda audit: build_server(Retriever(load_kb(KB_DIR)), audit=audit), test, "policies"
    )


def test_server_search_cites_sources():
    async def t(client: ClientSession) -> None:
        found = payload(await client.call_tool("search_policies", {"query": "express shipping cost"}))
        top = found["results"][0]
        assert top["doc_id"] == "shipping-policy"
        assert top["url"].startswith("https://") and top["status"] == "current"
        old = payload(
            await client.call_tool(
                "search_policies", {"query": "return unopened coffee days", "as_of": "2025-11-02"}
            )
        )
        ids = [r["doc_id"] for r in old["results"]]
        assert "refund-policy-v1" in ids and "refund-policy" not in ids
        assert "2025-11-02" in old["notes"][0]

    run(t)


def test_server_rejects_bad_input():
    async def t(client: ClientSession) -> None:
        assert error_text(await client.call_tool("search_policies", {"query": "  "}))
        assert error_text(await client.call_tool("search_policies", {"query": "refund", "k": 50}))
        assert "date" in error_text(
            await client.call_tool("search_policies", {"query": "x", "as_of": "last week"})
        )
        assert "No document" in error_text(await client.call_tool("get_policy", {"doc_id": "nope"}))

    run(t)


def test_server_documents():
    async def t(client: ClientSession) -> None:
        listed = payload(await client.call_tool("list_policies", {}))
        assert len(listed) == 39
        doc = payload(await client.call_tool("get_policy", {"doc_id": "refund-policy-v1"}))
        assert doc["superseded_by"] == "refund-policy"
        res = await client.read_resource("kb://docs/gift-cards")
        assert "Gift card" in res.contents[0].text

    run(t)


def test_threshold_is_tuned_on_dev_only():
    from copilot.rag.evaluate import tune_threshold

    def row(split, answerable, score):
        return {"id": "x", "split": split, "answerable": answerable, "top_score": score}

    rows = [row("dev", True, s) for s in (0.9, 0.6, 0.4)] + [row("dev", False, s) for s in (0.05, 0.1)]
    rows += [row("test", True, 0.02)]  # a hard test question must not move the dev-chosen cutoff
    tuned = tune_threshold(rows)
    assert 0.1 < tuned["threshold"] < 0.4
    assert tuned["dev_accuracy"] == 1.0
    assert tuned["test_accuracy"] == 0.0
