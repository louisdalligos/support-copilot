"""Build the whole dataset and write it to disk deterministically."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from copilot.seed.catalog import build_catalog
from copilot.seed.common import Dataset
from copilot.seed.evals import build_eval_tickets, build_rag_questions
from copilot.seed.orders import build_order_history
from copilot.seed.people import build_customers
from copilot.seed.tickets import build_past_tickets

# Change this only on purpose: it changes every generated file.
SEED = "northwind-seed-v1"

FILES = {
    "products.jsonl": "products",
    "customers.jsonl": "customers",
    "orders.jsonl": "orders",
    "transactions.jsonl": "transactions",
    "refunds.jsonl": "refunds",
    "disputes.jsonl": "disputes",
    "tickets.jsonl": "tickets",
    "eval_tickets.jsonl": "eval_tickets",
    "rag_questions.jsonl": "rag_questions",
}


def build_dataset(seed: str = SEED) -> Dataset:
    rng = random.Random(seed)
    ds = Dataset()
    build_catalog(ds)
    build_customers(ds, rng)
    build_order_history(ds, rng)
    build_past_tickets(ds, rng)
    # Eval scenarios add their own customers and orders after the history, so
    # adding a scenario never changes the background data.
    build_eval_tickets(ds, random.Random(f"{seed}-evals"))
    build_rag_questions(ds)
    return ds


def render(ds: Dataset) -> dict[str, str]:
    """Each file as text: one JSON object per line, sorted keys, LF endings."""
    out = {}
    for filename, attr in FILES.items():
        rows: list[dict[str, Any]] = getattr(ds, attr)
        out[filename] = "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows)
    return out


def write(ds: Dataset, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, text in render(ds).items():
        (out_dir / filename).write_text(text, encoding="utf-8", newline="\n")


def check(ds: Dataset, out_dir: Path) -> list[str]:
    """Names of files on disk that differ from a fresh build."""
    stale = []
    for filename, text in render(ds).items():
        path = out_dir / filename
        on_disk = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else None
        if on_disk != text:
            stale.append(filename)
    return stale


def load(out_dir: Path) -> dict[str, list[dict[str, Any]]]:
    data = {}
    for filename, attr in FILES.items():
        lines = (out_dir / filename).read_text(encoding="utf-8").splitlines()
        data[attr] = [json.loads(line) for line in lines if line.strip()]
    return data
