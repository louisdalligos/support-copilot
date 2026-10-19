"""Shopify backends.

The MCP server talks to a ShopifyBackend. FakeShopify serves the seed data in
memory. A backend for the real Shopify Admin API implements the same
interface later, so the server does not change.

Refunds are never issued here. The agent can only file a refund proposal,
which waits for a person to approve it.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol


class NotFound(LookupError):
    pass


class ShopifyBackend(Protocol):
    def order(self, name: str) -> dict[str, Any]: ...
    def transactions(self, order_id: int) -> list[dict[str, Any]]: ...
    def refunds(self, order_id: int) -> list[dict[str, Any]]: ...
    def disputes(self, order_id: int) -> list[dict[str, Any]]: ...
    def customer(self, email: str) -> dict[str, Any]: ...
    def orders_for(self, email: str, *, limit: int) -> list[dict[str, Any]]: ...
    def transactions_matching(self, amount: Decimal, *, limit: int) -> list[dict[str, Any]]: ...
    def file_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]: ...
    def proposals(self, order_id: int | None = None) -> list[dict[str, Any]]: ...


def order_key(name: str) -> str:
    """'#1792', '1792' and ' #1792 ' all name the same order."""
    return "#" + name.strip().lstrip("#")


class FakeShopify:
    """An in-memory store. Proposals last for the life of the process only."""

    def __init__(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self._orders = {o["name"]: o for o in data["orders"]}
        self._customers = {c["email"].lower(): c for c in data["customers"]}

        def by_order(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
            out: dict[int, list[dict[str, Any]]] = {}
            for r in rows:
                out.setdefault(r["order_id"], []).append(r)
            return out

        self._tx = by_order(data["transactions"])
        self._refunds = by_order(data["refunds"])
        self._disputes = by_order(data["disputes"])
        self._all_tx = data["transactions"]
        self._orders_by_id = {o["id"]: o for o in data["orders"]}
        self._proposals: list[dict[str, Any]] = []

    @classmethod
    def from_generated(cls, data_dir: Path) -> FakeShopify:
        def rows(name: str) -> list[dict[str, Any]]:
            text = (data_dir / f"{name}.jsonl").read_text(encoding="utf-8")
            return [json.loads(line) for line in text.splitlines() if line]

        names = ("orders", "customers", "transactions", "refunds", "disputes")
        return cls({n: rows(n) for n in names})

    def order(self, name: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self._orders[order_key(name)])
        except KeyError:
            raise NotFound(f"Order {order_key(name)} not found") from None

    def transactions(self, order_id: int) -> list[dict[str, Any]]:
        return copy.deepcopy(self._tx.get(order_id, []))

    def refunds(self, order_id: int) -> list[dict[str, Any]]:
        return copy.deepcopy(self._refunds.get(order_id, []))

    def disputes(self, order_id: int) -> list[dict[str, Any]]:
        return copy.deepcopy(self._disputes.get(order_id, []))

    def customer(self, email: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self._customers[email.strip().lower()])
        except KeyError:
            raise NotFound("No customer with that email") from None

    def orders_for(self, email: str, *, limit: int = 20) -> list[dict[str, Any]]:
        email = email.strip().lower()
        hits = [o for o in self._orders.values() if o["email"].lower() == email]
        hits.sort(key=lambda o: o["created_at"], reverse=True)
        return copy.deepcopy(hits[:limit])

    def transactions_matching(self, amount: Decimal, *, limit: int = 20) -> list[dict[str, Any]]:
        hits = [t for t in self._all_tx if t["kind"] == "sale" and Decimal(t["amount"]) == amount]
        hits.sort(key=lambda t: t["created_at"], reverse=True)
        out = []
        for t in hits[:limit]:
            row = dict(t)
            row["order_name"] = self._orders_by_id[t["order_id"]]["name"]
            out.append(row)
        return out

    def file_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        filed = {
            **proposal,
            "id": len(self._proposals) + 1,
            "status": "pending_approval",
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self._proposals.append(filed)
        return copy.deepcopy(filed)

    def proposals(self, order_id: int | None = None) -> list[dict[str, Any]]:
        rows = [p for p in self._proposals if order_id is None or p["order_id"] == order_id]
        return copy.deepcopy(rows)
