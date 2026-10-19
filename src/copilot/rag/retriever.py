"""Hybrid retrieval: BM25 + dense embeddings, fused with RRF, then reranked.

Policy versions: superseded documents stay in the index but are left out of
results by default. When the question is about an older order, pass `as_of`
(for example the delivery date) and the version in force on that date is
used instead, so an order delivered under the old 14-day policy gets the old
policy, and everything newer gets the current one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from copilot.kb import KbDoc, load_kb
from copilot.rag.chunks import Chunk, chunk_docs
from copilot.rag.lexical import BM25
from copilot.rag.models import Embedder, Reranker

RRF_K = 60  # the usual constant; it damps the influence of the very top ranks
CANDIDATES = 10  # fused results the reranker rescores; 20 was no more accurate and twice as slow
# No "nothing relevant" cutoff by default. Measured on the golden questions, the reranker's
# best score does not separate answerable from unanswerable questions (some correct answers
# score lower than off-topic ones), so the agent judges whether the passages answer the question.
DEFAULT_THRESHOLD: float | None = None


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float  # reranker probability if reranked, otherwise the fused RRF score
    lexical_rank: int | None
    dense_rank: int | None


@dataclass
class SearchResult:
    query: str
    hits: list[Hit]
    pipeline: str
    # True/False only when a threshold is configured; None means "not judged here".
    relevant: bool | None = None
    top_score: float | None = None  # the reranker's best score, used to tune the threshold
    notes: list[str] = field(default_factory=list)

    @property
    def doc_ids(self) -> list[str]:
        """Documents in rank order, each once."""
        seen: list[str] = []
        for h in self.hits:
            if h.chunk.doc_id not in seen:
                seen.append(h.chunk.doc_id)
        return seen


def _family(chunk: Chunk) -> str:
    return chunk.superseded_by or chunk.doc_id


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class Retriever:
    def __init__(
        self,
        docs: dict[str, KbDoc] | list[KbDoc],
        *,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        threshold: float | None = DEFAULT_THRESHOLD,
        candidates: int = CANDIDATES,
    ) -> None:
        self.docs = docs if isinstance(docs, dict) else {d.id: d for d in docs}
        self.chunks = chunk_docs(self.docs)
        self.bm25 = BM25([c.text for c in self.chunks])
        self.embedder = embedder
        self.reranker = reranker
        self.threshold = threshold
        self.candidates = candidates
        self.vectors = embedder.embed_documents([c.text for c in self.chunks]) if embedder else None
        families: dict[str, list[Chunk]] = {}
        for c in self.chunks:
            families.setdefault(_family(c), []).append(c)
        self._versioned = {f for f, cs in families.items() if len({c.doc_id for c in cs}) > 1}

    @classmethod
    def from_kb_dir(cls, kb_dir: Path, **kwargs) -> Retriever:  # noqa: ANN003
        return cls(load_kb(kb_dir), **kwargs)

    @property
    def pipeline(self) -> str:
        parts = ["bm25"]
        if self.embedder:
            parts.append(f"dense({self.embedder.name})")
        if self.reranker:
            parts.append(f"rerank({self.reranker.name})")
        return " + ".join(parts)

    def _allowed(self, chunk: Chunk, as_of: date | None, include_superseded: bool) -> bool:
        if include_superseded:
            return True
        family = _family(chunk)
        if as_of is None or family not in self._versioned:
            return chunk.is_current
        # The version in force on as_of: the latest one that took effect on or before it.
        versions = sorted(
            {(c.effective_date, c.doc_id) for c in self.chunks if _family(c) == family}, reverse=True
        )
        in_force = next((d for eff, d in versions if date.fromisoformat(eff) <= as_of), versions[-1][1])
        return chunk.doc_id == in_force

    def search(
        self,
        query: str,
        *,
        k: int = 5,
        as_of: date | None = None,
        include_superseded: bool = False,
    ) -> SearchResult:
        allowed = [i for i, c in enumerate(self.chunks) if self._allowed(c, as_of, include_superseded)]

        lexical = self.bm25.scores(query)
        lex_order = [i for i in sorted(allowed, key=lambda i: -lexical[i]) if lexical[i] > 0]
        lex_rank = {i: r for r, i in enumerate(lex_order, 1)}

        dense_rank: dict[int, int] = {}
        if self.embedder and self.vectors is not None:
            q = self.embedder.embed_query(query)
            sims = {i: _dot(q, self.vectors[i]) for i in allowed}
            dense_rank = {i: r for r, i in enumerate(sorted(allowed, key=lambda i: -sims[i]), 1)}

        fused = {
            i: sum(1 / (RRF_K + ranks[i]) for ranks in (lex_rank, dense_rank) if i in ranks) for i in allowed
        }
        order = [i for i in sorted(allowed, key=lambda i: -fused[i]) if fused[i] > 0]

        result = SearchResult(query=query, hits=[], pipeline=self.pipeline)
        if self.reranker:
            pool = order[: self.candidates]
            scores = self.reranker.score(query, [self.chunks[i].text for i in pool])
            ranked = sorted(zip(pool, scores, strict=True), key=lambda p: -p[1])
            result.hits = [Hit(self.chunks[i], s, lex_rank.get(i), dense_rank.get(i)) for i, s in ranked[:k]]
            result.top_score = ranked[0][1] if ranked else 0.0
            if self.threshold is not None:
                result.relevant = result.top_score >= self.threshold
        else:
            result.hits = [
                Hit(self.chunks[i], fused[i], lex_rank.get(i), dense_rank.get(i)) for i in order[:k]
            ]

        if as_of is not None:
            result.notes.append(f"Policy versions in force on {as_of.isoformat()}.")
        for h in result.hits:
            if not h.chunk.is_current:
                result.notes.append(f"{h.chunk.doc_id} is superseded by {h.chunk.superseded_by}.")
        result.notes = list(dict.fromkeys(result.notes))
        return result
