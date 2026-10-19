"""Command line for the seed data.

python -m copilot.seed             regenerate data/generated
python -m copilot.seed --check     fail if data/generated is out of date (used in CI)
python -m copilot.seed --sample 5  print random tickets to review by hand
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from pathlib import Path

from copilot.seed.build import build_dataset, check, write

ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = ROOT / "data" / "generated"


def summary(ds) -> str:
    evals = ds.eval_tickets
    lines = [
        f"products        {len(ds.products)}",
        f"customers       {len(ds.customers)}",
        f"orders          {len(ds.orders)}",
        f"transactions    {len(ds.transactions)}",
        f"refunds         {len(ds.refunds)}",
        f"disputes        {len(ds.disputes)}",
        f"past tickets    {len(ds.tickets)}",
        f"eval tickets    {len(evals)} "
        f"(dev {sum(e['split'] == 'dev' for e in evals)}, test {sum(e['split'] == 'test' for e in evals)})",
        f"rag questions   {len(ds.rag_questions)}",
        "eval actions    "
        + ", ".join(
            f"{k} {v}" for k, v in sorted(Counter(e["labels"]["expected_action"] for e in evals).items())
        ),
    ]
    return "\n".join(lines)


def print_sample(ds, n: int) -> None:
    rng = random.Random()
    pool = [("past", t) for t in ds.tickets] + [("eval", e["ticket"]) for e in ds.eval_tickets]
    for kind, ticket in rng.sample(pool, k=min(n, len(pool))):
        print(f"--- {kind} ticket {ticket['id']}: {ticket['subject']}")
        for comment in ticket["comments"]:
            print(f"[{comment['author']}] {comment['body']}")
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if generated files are stale")
    parser.add_argument("--sample", type=int, metavar="N", help="print N random tickets for review")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    ds = build_dataset()
    if args.sample:
        print_sample(ds, args.sample)
        return 0
    if args.check:
        stale = check(ds, args.out)
        if stale:
            print("Out of date, run `python -m copilot.seed`: " + ", ".join(stale), file=sys.stderr)
            return 1
        print("Generated data is up to date.")
        return 0
    write(ds, args.out)
    print(f"Wrote {args.out}\n{summary(ds)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
