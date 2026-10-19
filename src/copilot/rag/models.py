"""Open models from Hugging Face for dense search and reranking.

Both run locally on CPU through sentence-transformers, which is an optional
dependency: install it with `pip install -e ".[rag]"`. Without it, retrieval
falls back to keyword search only.

Defaults:
- Embeddings: google/embeddinggemma-300m. Small (300M parameters), strong on
  retrieval benchmarks, and uses separate prompts for queries and documents.
  It is a gated model: accept its license on Hugging Face and log in first.
- Reranker: BAAI/bge-reranker-v2-m3. A cross-encoder that reads the query and
  a chunk together, which is slower than embeddings but much more precise.
  Its scores are probabilities, so a threshold can say "nothing relevant".
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

DEFAULT_EMBEDDER = "google/embeddinggemma-300m"
DEFAULT_RERANKER = "BAAI/bge-reranker-v2-m3"


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class Reranker(Protocol):
    name: str

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """Relevance of each text to the query, between 0 and 1."""
        ...


def _require_sentence_transformers():  # noqa: ANN202
    try:
        import sentence_transformers
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            'Dense search and reranking need sentence-transformers: pip install -e ".[rag]"'
        ) from exc
    return sentence_transformers


class SentenceTransformerEmbedder:
    def __init__(self, name: str = DEFAULT_EMBEDDER, device: str | None = None) -> None:
        st = _require_sentence_transformers()
        self.name = name
        self.model = st.SentenceTransformer(name, device=device)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self.model.encode_document(list(texts), normalize_embeddings=True, batch_size=16)
        return [v.tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.model.encode_query([text], normalize_embeddings=True)[0].tolist()


class CrossEncoderReranker:
    def __init__(self, name: str = DEFAULT_RERANKER, device: str | None = None) -> None:
        st = _require_sentence_transformers()
        self.name = name
        # Chunks are short; capping the length keeps CPU inference fast.
        self.model = st.CrossEncoder(name, device=device, max_length=512)

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        raw = [float(s) for s in self.model.predict([(query, t) for t in texts], batch_size=16)]
        if all(0.0 <= s <= 1.0 for s in raw):
            return raw
        return [1 / (1 + math.exp(-s)) for s in raw]  # logits: turn them into probabilities
