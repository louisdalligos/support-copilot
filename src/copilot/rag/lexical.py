"""BM25 keyword search, in plain Python.

The knowledge base is small, so an in-memory index is fast and needs no
dependencies. In deployment the same role is played by Postgres full-text
search; this version keeps tests and CI independent of a database.
"""

from __future__ import annotations

import math
import re
from collections import Counter

WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
STOPWORDS = frozenset(
    """a an and are as at be but by can could do does did for from had has have how i if in is it its
    me my of on or our so than that the their them then there these they this to was we were what
    when where which who why will with would you your i'm i've it's don't can't won't""".split()  # noqa: SIM905
)


def stem(word: str) -> str:
    """A deliberately small stemmer: enough to match 'refunds' to 'refund' and 'charged' to 'charge'."""
    word = word.split("'")[0]
    for suffix, keep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            if suffix == "s" and word.endswith("ss"):
                return word
            return word[: -len(suffix)] + keep
    return word


def tokenize(text: str) -> list[str]:
    return [stem(w) for w in WORD.findall(text.lower()) if w not in STOPWORDS]


class BM25:
    def __init__(self, texts: list[str], *, k1: float = 1.2, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(t)) for t in texts]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg_len = sum(self.lengths) / max(len(self.docs), 1)
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = [t for t in tokenize(query) if t in self.idf]
        out = []
        for doc, length in zip(self.docs, self.lengths, strict=True):
            s = 0.0
            for t in terms:
                tf = doc.get(t, 0)
                if tf:
                    norm = tf + self.k1 * (1 - self.b + self.b * length / self.avg_len)
                    s += self.idf[t] * tf * (self.k1 + 1) / norm
            out.append(s)
        return out
