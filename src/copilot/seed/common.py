"""Shared helpers for the seed generator: ids, money, dates, messy text."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CENT = Decimal("0.01")


def money(value: Decimal | str | int) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_money(value: Decimal) -> str:
    """Amounts are stored as strings, the way Shopify's API returns them."""
    return f"{money(value):.2f}"


def iso_dt(day: date, rng: random.Random, start_hour: int = 7, end_hour: int = 22) -> str:
    moment = datetime.combine(day, time(rng.randint(start_hour, end_hour - 1), rng.randint(0, 59)))
    return moment.strftime("%Y-%m-%dT%H:%M:00Z")


def add_days(day: date, days: int) -> date:
    return day + timedelta(days=days)


def human_date(day: date) -> str:
    return f"{day.strftime('%B')} {day.day}"


@dataclass
class Ids:
    """Monotonic id counters, so ids are stable and readable."""

    counters: dict[str, int] = field(default_factory=dict)
    starts: dict[str, int] = field(
        default_factory=lambda: {
            "product": 100,
            "customer": 2000,
            "order": 5000,
            "line_item": 70000,
            "transaction": 90000,
            "refund": 30000,
            "dispute": 4000,
            "ticket": 10000,
        }
    )

    def next(self, kind: str) -> int:
        value = self.counters.get(kind, self.starts[kind]) + 1
        self.counters[kind] = value
        return value


@dataclass
class Dataset:
    products: list[dict[str, Any]] = field(default_factory=list)
    customers: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    refunds: list[dict[str, Any]] = field(default_factory=list)
    disputes: list[dict[str, Any]] = field(default_factory=list)
    tickets: list[dict[str, Any]] = field(default_factory=list)
    eval_tickets: list[dict[str, Any]] = field(default_factory=list)
    rag_questions: list[dict[str, Any]] = field(default_factory=list)
    ids: Ids = field(default_factory=Ids)

    def product(self, product_id: int) -> dict[str, Any]:
        return next(p for p in self.products if p["id"] == product_id)

    def customer(self, customer_id: int) -> dict[str, Any]:
        return next(c for c in self.customers if c["id"] == customer_id)


def messy(text: str, rng: random.Random, *, shout: bool = True) -> str:
    """Make a ticket read like a real one: typos, no capitals, shouting."""
    ops = rng.sample(
        ["typos", "lowercase", "shout", "no_punct"] if shout else ["typos", "lowercase", "no_punct"], k=2
    )
    if "typos" in ops:
        words = text.split(" ")
        candidates = [i for i, w in enumerate(words) if len(w) > 4 and w.isalpha()]
        for i in rng.sample(candidates, k=min(2, len(candidates))):
            w = words[i]
            j = rng.randint(1, len(w) - 3)
            words[i] = w[:j] + w[j + 1] + w[j] + w[j + 2 :]
        text = " ".join(words)
    if "lowercase" in ops:
        text = text.lower()
    if "shout" in ops:
        text = text.rstrip(".") + "!!! this is really frustrating"
    if "no_punct" in ops:
        text = text.replace(",", "").replace("'", "")
    return text
