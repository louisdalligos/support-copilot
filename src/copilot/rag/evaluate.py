"""Retrieval evals over the golden questions in data/generated/rag_questions.jsonl.

Metrics are computed per document, not per chunk:
- hit@1, hit@5: a relevant document is ranked first / in the top 5.
- recall@5: the share of a question's relevant documents found in the top 5.
- MRR: 1 / rank of the first relevant document (0 if none in the top 10).
- stale: answers that would cite a superseded policy (should always be 0).
- abstain accuracy (reranked pipelines only): unanswerable questions are
  flagged as having nothing relevant, answerable ones are not.

    python -m copilot.rag.evaluate                      # BM25 only, no downloads
    python -m copilot.rag.evaluate --dense --rerank     # the full hybrid pipeline
    python -m copilot.rag.evaluate --min-hit5 0.85      # fail (exit 1) below a threshold, for CI
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from copilot.rag.retriever import Retriever

ROOT = Path(__file__).resolve().parents[3]
QUESTIONS = ROOT / "data" / "generated" / "rag_questions.jsonl"
KB_DIR = ROOT / "data" / "kb"


@dataclass
class Report:
    pipeline: str
    split: str
    questions: int
    hit_at_1: float
    hit_at_5: float
    recall_at_5: float
    mrr: float
    stale: int
    abstain_accuracy: float | None
    seconds_per_query: float
    misses: list[dict[str, Any]]
    # Reranked pipelines: each question's best reranker score, and the threshold tuned on dev.
    scores: list[dict[str, Any]] | None = None
    tuned_threshold: dict[str, Any] | None = None


def load_questions(path: Path = QUESTIONS, split: str = "all") -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [q for q in rows if split == "all" or q["split"] == split]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def evaluate(
    retriever: Retriever, questions: list[dict[str, Any]], split: str = "all", *, progress: bool = False
) -> Report:
    answerable = [q for q in questions if q["answerable"]]
    hit1 = hit5 = recall5 = mrr = 0.0
    stale = 0
    abstain_right = abstain_total = 0
    scores: list[dict[str, Any]] = []
    misses = []
    started = time.perf_counter()

    for n_done, q in enumerate(questions, 1):
        if progress:
            log(f"  question {n_done}/{len(questions)}  ({time.perf_counter() - started:.0f}s)")
        result = retriever.search(q["question"], k=10)
        if result.top_score is not None:
            scores.append(
                {
                    "id": q["id"],
                    "split": q["split"],
                    "answerable": q["answerable"],
                    "top_score": round(result.top_score, 4),
                }
            )
        docs = result.doc_ids
        stale += sum(1 for h in result.hits[:5] if not h.chunk.is_current)
        if result.relevant is not None:
            abstain_total += 1
            abstain_right += result.relevant == q["answerable"]
        if not q["answerable"]:
            continue
        relevant = set(q["relevant_doc_ids"])
        hit1 += bool(docs[:1]) and docs[0] in relevant
        hit5 += bool(relevant & set(docs[:5]))
        recall5 += len(relevant & set(docs[:5])) / len(relevant)
        first = next((r for r, d in enumerate(docs[:10], 1) if d in relevant), None)
        mrr += 1 / first if first else 0.0
        if not relevant & set(docs[:5]):
            misses.append(
                {"id": q["id"], "question": q["question"], "expected": q["relevant_doc_ids"], "got": docs[:5]}
            )

    n = max(len(answerable), 1)
    return Report(
        pipeline=retriever.pipeline,
        split=split,
        questions=len(questions),
        hit_at_1=round(hit1 / n, 3),
        hit_at_5=round(hit5 / n, 3),
        recall_at_5=round(recall5 / n, 3),
        mrr=round(mrr / n, 3),
        stale=stale,
        abstain_accuracy=round(abstain_right / abstain_total, 3) if abstain_total else None,
        seconds_per_query=round((time.perf_counter() - started) / max(len(questions), 1), 4),
        misses=misses,
        scores=scores or None,
        tuned_threshold=tune_threshold(scores) if scores else None,
    )


def _accuracy(rows: list[dict[str, Any]], threshold: float) -> float:
    right = sum((r["top_score"] >= threshold) == r["answerable"] for r in rows)
    return right / len(rows) if rows else 0.0


def tune_threshold(scores: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the "nothing relevant" cutoff on dev questions only, then report it on test.

    Candidates are midpoints between neighbouring scores. Ties go to the lowest
    cutoff, so the agent would rather answer from weak evidence it can cite than
    escalate a question the documents do answer; the refund rules in code catch
    anything that matters for money.
    """
    dev = [r for r in scores if r["split"] == "dev"]
    test = [r for r in scores if r["split"] == "test"]
    values = sorted({r["top_score"] for r in dev})
    candidates = [0.0] + [(a + b) / 2 for a, b in zip(values, values[1:], strict=False)] + [1.0]
    best = max(candidates, key=lambda t: (_accuracy(dev, t), -t))
    return {
        "threshold": round(best, 4),
        "dev_accuracy": round(_accuracy(dev, best), 3),
        "test_accuracy": round(_accuracy(test, best), 3) if test else None,
        "lowest_answerable": min((r["top_score"] for r in scores if r["answerable"]), default=None),
        "highest_unanswerable": max((r["top_score"] for r in scores if not r["answerable"]), default=None),
    }


def build_retriever(args: argparse.Namespace) -> Retriever:
    embedder = reranker = None
    if args.dense:
        from copilot.rag.models import SentenceTransformerEmbedder

        log(f"Loading embedder {args.embedder} (downloads on first use)...")
        t = time.perf_counter()
        embedder = SentenceTransformerEmbedder(args.embedder)
        log(f"  loaded in {time.perf_counter() - t:.0f}s")
    if args.rerank:
        from copilot.rag.models import CrossEncoderReranker

        log(f"Loading reranker {args.reranker} (downloads on first use)...")
        t = time.perf_counter()
        reranker = CrossEncoderReranker(args.reranker)
        log(f"  loaded in {time.perf_counter() - t:.0f}s")
    if embedder:
        log("Embedding the knowledge base...")
    t = time.perf_counter()
    retriever = Retriever.from_kb_dir(
        KB_DIR, embedder=embedder, reranker=reranker, threshold=args.threshold, candidates=args.candidates
    )
    if embedder:
        log(f"  {len(retriever.chunks)} chunks embedded in {time.perf_counter() - t:.0f}s")
    return retriever


def main(argv: list[str] | None = None) -> int:
    from copilot.rag.models import DEFAULT_EMBEDDER, DEFAULT_RERANKER
    from copilot.rag.retriever import CANDIDATES, DEFAULT_THRESHOLD

    parser = argparse.ArgumentParser(
        prog="python -m copilot.rag.evaluate",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dense", action="store_true", help="add dense embeddings (needs the rag extra)")
    parser.add_argument(
        "--rerank", action="store_true", help="add the cross-encoder reranker (needs the rag extra)"
    )
    parser.add_argument("--embedder", default=DEFAULT_EMBEDDER)
    parser.add_argument("--reranker", default=DEFAULT_RERANKER)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="score an abstain cutoff")
    parser.add_argument(
        "--candidates", type=int, default=CANDIDATES, help="fused results the reranker rescores (default 20)"
    )
    parser.add_argument("--split", choices=["dev", "test", "all"], default="all")
    parser.add_argument("--out", type=Path, help="write the report as JSON")
    parser.add_argument("--min-hit5", type=float, help="exit 1 if hit@5 is below this")
    parser.add_argument("--min-abstain", type=float, help="exit 1 if abstain accuracy is below this")
    args = parser.parse_args(argv)

    retriever = build_retriever(args)
    questions = load_questions(split=args.split)
    if args.dense or args.rerank:
        log(f"Scoring {len(questions)} questions...")
    report = evaluate(retriever, questions, args.split, progress=args.dense or args.rerank)

    print(f"pipeline   {report.pipeline}")
    print(f"split      {report.split} ({report.questions} questions)")
    for name in (
        "hit_at_1",
        "hit_at_5",
        "recall_at_5",
        "mrr",
        "stale",
        "abstain_accuracy",
        "seconds_per_query",
    ):
        print(f"{name:<18} {getattr(report, name)}")
    if report.tuned_threshold:
        t = report.tuned_threshold
        print(
            f"tuned threshold    {t['threshold']} (dev accuracy {t['dev_accuracy']}, test {t['test_accuracy']}; "
            f"lowest answerable score {t['lowest_answerable']}, highest unanswerable {t['highest_unanswerable']})"
        )
        cutoff = t["threshold"] if args.threshold is None else args.threshold
        wrong = [r for r in report.scores or [] if (r["top_score"] >= cutoff) != r["answerable"]]
        for r in wrong:
            kind = "answerable but below" if r["answerable"] else "unanswerable but above"
            print(f"  abstain wrong {r['id']} ({r['split']}): {kind} {cutoff}, top score {r['top_score']}")
    for m in report.misses:
        print(f"  miss {m['id']}: {m['question']!r} expected {m['expected']}, got {m['got']}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")

    failed = []
    if report.stale:
        failed.append(f"{report.stale} results cite a superseded policy")
    if args.min_hit5 is not None and report.hit_at_5 < args.min_hit5:
        failed.append(f"hit@5 {report.hit_at_5} is below {args.min_hit5}")
    if args.min_abstain is not None and (report.abstain_accuracy or 0) < args.min_abstain:
        failed.append(f"abstain accuracy {report.abstain_accuracy} is below {args.min_abstain}")
    for f in failed:
        print(f"FAIL: {f}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
