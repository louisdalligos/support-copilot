"""A real Shopify store, through the Admin GraphQL API.

LiveShopify implements the same ShopifyBackend interface as FakeShopify and returns
the same shapes, so the MCP server, the agent and the approval app do not change.

Sign-in uses the client credentials grant: an app made in Shopify's Dev Dashboard
and installed on a store in the same organization exchanges its client ID and
secret for an access token that lasts 24 hours. The token is cached and renewed a
minute before it expires.

Refund proposals have no equivalent in Shopify, so they are kept on the order itself,
in a JSON metafield (namespace "copilot", key "refund_proposals"). They survive
restarts and are visible in the Shopify admin. A proposal's id encodes its order's id
(order_id * 1000 + n), so it can be found again from the id alone.

Settings (environment variables):
    SHOPIFY_STORE_DOMAIN    your-store.myshopify.com
    SHOPIFY_CLIENT_ID       from the app's Settings page in the Dev Dashboard
    SHOPIFY_CLIENT_SECRET
    SHOPIFY_API_VERSION     optional, default below
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from copilot.shopify.backend import NotFound, order_key
from copilot.store import DESCRIPTOR_ORDER, DESCRIPTOR_SUBSCRIPTION

API_VERSION = "2026-07"
PROPOSALS_NAMESPACE = "copilot"
PROPOSALS_KEY = "refund_proposals"
FINAL_SALE_TAG = "final-sale"


class ShopifyError(RuntimeError):
    """Shopify refused a request; the message carries its errors."""


def gid(kind: str, legacy_id: int | str) -> str:
    return f"gid://shopify/{kind}/{legacy_id}"


def legacy(global_id: str | None) -> int | None:
    """gid://shopify/Order/123 -> 123."""
    if not global_id:
        return None
    return int(global_id.rsplit("/", 1)[-1].split("?")[0])


def _amount(money_set: dict[str, Any] | None) -> str:
    if not money_set:
        return "0.00"
    return str(Decimal(money_set["shopMoney"]["amount"]).quantize(Decimal("0.01")))


def _date(ts: str | None) -> str | None:
    return ts[:10] if ts else None


def _ts(ts: str | None) -> str | None:
    """Shopify's ISO timestamps, in the seed data's form: 2026-10-15T07:11:00Z."""
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def store_domain(value: str) -> str:
    """The store's myshopify.com host, from what people paste: 'my-store', 'my-store.myshopify.com',
    'https://my-store.myshopify.com/', or an admin URL like 'admin.shopify.com/store/my-store'."""
    v = value.strip().removeprefix("https://").removeprefix("http://").strip("/")
    if v.startswith("admin.shopify.com/store/"):
        v = v.split("/")[2]
    v = v.split("/")[0]
    return v if v.endswith(".myshopify.com") else f"{v}.myshopify.com"


def _readable(body: str) -> str:
    """Shopify answers some sign-in errors with an HTML page; keep its message, not the markup."""
    text = body.strip()
    if text.startswith("{"):
        return text[:300]
    title = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
    words = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", text, flags=re.S | re.I)
    words = " ".join(html.unescape(words).split())
    return ((title.group(1).strip() + ": ") if title else "") + words[:300]


class AdminClient:
    """Shopify Admin GraphQL API with client-credentials sign-in."""

    def __init__(
        self,
        shop: str,
        client_id: str,
        client_secret: str,
        *,
        api_version: str = API_VERSION,
        http: httpx.Client | None = None,
    ) -> None:
        self.shop = store_domain(shop)
        self._client_id = client_id
        self._client_secret = client_secret
        self.api_version = api_version
        self._http = http or httpx.Client(timeout=30)
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> AdminClient:
        missing = [
            k
            for k in ("SHOPIFY_STORE_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET")
            if not os.environ.get(k)
        ]
        if missing:
            raise SystemExit(f"Live Shopify needs {', '.join(missing)} (see docs/shopify.md).")
        return cls(
            os.environ["SHOPIFY_STORE_DOMAIN"],
            os.environ["SHOPIFY_CLIENT_ID"],
            os.environ["SHOPIFY_CLIENT_SECRET"],
            api_version=os.environ.get("SHOPIFY_API_VERSION", API_VERSION),
        )

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            r = self._http.post(
                f"https://{self.shop}/admin/oauth/access_token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            if r.status_code != 200:
                raise ShopifyError(
                    f"Shopify sign-in to {self.shop} failed ({r.status_code}): {_readable(r.text)}. "
                    "Check SHOPIFY_STORE_DOMAIN is the store's myshopify.com address, the client ID and "
                    "secret are from the app's Settings page, and the app is installed on that store."
                )
            body = r.json()
            self._token = body["access_token"]
            self._expires = time.monotonic() + float(body.get("expires_in", 86399)) - 60
            return self._token

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(4):
            r = self._http.post(
                f"https://{self.shop}/admin/api/{self.api_version}/graphql.json",
                json={"query": query, "variables": variables or {}},
                headers={"X-Shopify-Access-Token": self._access_token()},
            )
            if r.status_code == 401 and attempt == 0:
                self._token = None  # expired early or revoked: sign in again once
                continue
            if r.status_code in (429, 502, 503) and attempt < 3:
                time.sleep(2**attempt)
                continue
            if r.status_code != 200:
                raise ShopifyError(f"Shopify API error ({r.status_code}): {r.text[:300]}")
            body = r.json()
            errors = body.get("errors")
            if (
                errors
                and any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors)
                and attempt < 3
            ):
                time.sleep(2**attempt)
                continue
            if errors:
                raise ShopifyError("; ".join(e.get("message", str(e)) for e in errors))
            return body["data"]
        raise ShopifyError("Shopify kept throttling the request")

    def mutate(self, query: str, variables: dict[str, Any], field: str) -> dict[str, Any]:
        """Run a mutation and fail on its userErrors."""
        result = self.graphql(query, variables)[field]
        problems = result.get("userErrors") or []
        if problems:
            raise ShopifyError(
                f"{field}: "
                + "; ".join(f"{'.'.join(p.get('field') or [])} {p['message']}".strip() for p in problems)
            )
        return result


ORDER_FIELDS = """
  id
  legacyResourceId
  name
  email
  createdAt
  processedAt
  currencyCode
  tags
  displayFinancialStatus
  displayFulfillmentStatus
  totalPriceSet { shopMoney { amount } }
  subtotalPriceSet { shopMoney { amount } }
  totalShippingPriceSet { shopMoney { amount } }
  customer { legacyResourceId }
  shippingAddress { countryCodeV2 }
  lineItems(first: 50) {
    nodes {
      id
      title
      quantity
      originalUnitPriceSet { shopMoney { amount } }
      product { legacyResourceId productType tags }
    }
  }
  fulfillments(first: 10) {
    createdAt
    deliveredAt
    inTransitAt
    events(first: 20) { nodes { status happenedAt } }
  }
"""

PAYMENT_FIELDS = """
  legacyResourceId
  createdAt
  processedAt
  tags
  transactions(first: 50) {
    id
    kind
    status
    gateway
    createdAt
    processedAt
    amountSet { shopMoney { amount } }
    parentTransaction { id }
    paymentDetails { ... on CardPaymentDetails { number } }
  }
  refunds(first: 50) {
    id
    legacyResourceId
    createdAt
    note
    totalRefundedSet { shopMoney { amount } }
    transactions(first: 10) { nodes { parentTransaction { id } } }
  }
"""

PROPOSALS_FIELD = f'metafield(namespace: "{PROPOSALS_NAMESPACE}", key: "{PROPOSALS_KEY}") {{ value }}'


class LiveShopify:
    """ShopifyBackend for a real store. See the module docstring."""

    def __init__(self, client: AdminClient) -> None:
        self.client = client

    @classmethod
    def from_env(cls) -> LiveShopify:
        return cls(AdminClient.from_env())

    # Reading

    def _order_node(self, name: str) -> dict[str, Any]:
        q = f"query($q: String!) {{ orders(first: 1, query: $q) {{ nodes {{ {ORDER_FIELDS} }} }} }}"
        name = order_key(name)
        nodes = self.client.graphql(q, {"q": f'name:"{name}"'})["orders"]["nodes"]
        nodes = [n for n in nodes if n["name"] == name]
        if not nodes:
            raise NotFound(f"Order {name} not found")
        return nodes[0]

    def _payments(self, order_id: int) -> dict[str, Any]:
        q = f"query($id: ID!) {{ order(id: $id) {{ {PAYMENT_FIELDS} {PROPOSALS_FIELD} }} }}"
        node = self.client.graphql(q, {"id": gid("Order", order_id)})["order"]
        if node is None:
            raise NotFound(f"Order {order_id} not found")
        return node

    def order(self, name: str) -> dict[str, Any]:
        return self._order(self._order_node(name))

    def _order(self, n: dict[str, Any]) -> dict[str, Any]:
        events = [e for f in n.get("fulfillments") or [] for e in f["events"]["nodes"]]
        delivered = [e["happenedAt"] for e in events if e["status"] == "DELIVERED" and e["happenedAt"]]
        delivered += [
            f["deliveredAt"] for f in n.get("fulfillments") or [] if f.get("deliveredAt") and not delivered
        ]
        moving = [
            e["happenedAt"] for e in events if e["status"] in ("IN_TRANSIT", "CONFIRMED", "LABEL_PRINTED")
        ]
        moving += [f["inTransitAt"] or f["createdAt"] for f in n.get("fulfillments") or [] if not moving]
        tags = [t for t in n.get("tags") or []]
        line_items = []
        for li in n["lineItems"]["nodes"]:
            product = li.get("product") or {}
            line_items.append(
                {
                    "id": legacy(li["id"]),
                    "title": li["title"],
                    "quantity": li["quantity"],
                    "price": _amount(li["originalUnitPriceSet"]),
                    "product_id": int(product["legacyResourceId"])
                    if product.get("legacyResourceId")
                    else None,
                    "product_type": (product.get("productType") or "").lower() or None,
                    "final_sale": FINAL_SALE_TAG in [t.lower() for t in product.get("tags") or []],
                }
            )
        customer = n.get("customer") or {}
        return {
            "id": int(n["legacyResourceId"]),
            "name": n["name"],
            "email": n.get("email") or "",
            "created_at": _ts(n.get("processedAt") or n["createdAt"]),
            "currency": n.get("currencyCode") or "USD",
            "customer_id": int(customer["legacyResourceId"]) if customer.get("legacyResourceId") else None,
            "financial_status": (n.get("displayFinancialStatus") or "").lower() or None,
            "fulfillment_status": "fulfilled"
            if n.get("displayFulfillmentStatus") == "FULFILLED"
            else "unfulfilled",
            "shipped_at": _date(min(moving)) if moving else None,
            "delivered_at": _date(max(delivered)) if delivered else None,
            "line_items": line_items,
            "subtotal": _amount(n.get("subtotalPriceSet")),
            "shipping": _amount(n.get("totalShippingPriceSet")),
            "total": _amount(n.get("totalPriceSet")),
            "shipping_country": (n.get("shippingAddress") or {}).get("countryCodeV2"),
            "tags": tags,
        }

    def _transactions(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        order_id = int(node["legacyResourceId"])
        descriptor = (
            DESCRIPTOR_SUBSCRIPTION
            if "subscription_renewal" in (node.get("tags") or [])
            else DESCRIPTOR_ORDER
        )
        out = []
        for t in node.get("transactions") or []:
            kind = t["kind"].lower()
            if kind == "capture":
                kind = "sale"  # money taken; the seed data calls both "sale"
            number = (t.get("paymentDetails") or {}).get("number") or ""
            digits = "".join(c for c in number if c.isdigit())
            out.append(
                {
                    "id": legacy(t["id"]),
                    "order_id": order_id,
                    "kind": kind,
                    "status": t["status"].lower(),
                    "amount": _amount(t["amountSet"]),
                    "gateway": t.get("gateway"),
                    "card_last4": digits[-4:] or None,
                    "descriptor": descriptor,
                    "created_at": _ts(t.get("processedAt") or t["createdAt"]),
                }
            )
        return out

    def _refunds(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        order_id = int(node["legacyResourceId"])
        out = []
        for r in node.get("refunds") or []:
            parents = [
                legacy((t.get("parentTransaction") or {}).get("id")) for t in r["transactions"]["nodes"]
            ]
            out.append(
                {
                    "id": int(r["legacyResourceId"]),
                    "order_id": order_id,
                    "transaction_id": next((p for p in parents if p), None),
                    "amount": _amount(r["totalRefundedSet"]),
                    "note": r.get("note") or "",
                    "created_at": _ts(r["createdAt"]),
                }
            )
        return out

    def transactions(self, order_id: int) -> list[dict[str, Any]]:
        return self._transactions(self._payments(order_id))

    def refunds(self, order_id: int) -> list[dict[str, Any]]:
        return self._refunds(self._payments(order_id))

    def disputes(self, order_id: int) -> list[dict[str, Any]]:
        # Disputes come from Shopify Payments, which development stores do not use.
        return []

    def list_disputes(self) -> list[dict[str, Any]]:
        # Same: a development store on the test gateway has no chargebacks to list.
        return []

    def customer(self, email: str) -> dict[str, Any]:
        q = """query($q: String!) { customers(first: 5, query: $q) { nodes {
              legacyResourceId email firstName lastName createdAt
              defaultAddress { countryCodeV2 provinceCode }
              emailMarketingConsent { marketingState }
            } } }"""
        email = email.strip().lower()
        nodes = self.client.graphql(q, {"q": f'email:"{email}"'})["customers"]["nodes"]
        nodes = [c for c in nodes if (c.get("email") or "").lower() == email]
        if not nodes:
            raise NotFound("No customer with that email")
        c = nodes[0]
        address = c.get("defaultAddress") or {}
        return {
            "id": int(c["legacyResourceId"]),
            "email": c["email"],
            "first_name": c.get("firstName"),
            "last_name": c.get("lastName"),
            "created_at": _ts(c["createdAt"]),
            "country": address.get("countryCodeV2"),
            "province": address.get("provinceCode"),
            "accepts_marketing": (c.get("emailMarketingConsent") or {}).get("marketingState") == "SUBSCRIBED",
        }

    def orders_for(self, email: str, *, limit: int = 20) -> list[dict[str, Any]]:
        q = f"""query($q: String!, $n: Int!) {{
              orders(first: $n, query: $q, sortKey: PROCESSED_AT, reverse: true) {{ nodes {{ {ORDER_FIELDS} }} }}
            }}"""
        email = email.strip().lower()
        nodes = self.client.graphql(q, {"q": f'email:"{email}"', "n": max(1, min(limit, 250))})["orders"][
            "nodes"
        ]
        return [self._order(n) for n in nodes if (n.get("email") or "").lower() == email]

    def transactions_matching(self, amount: Decimal, *, limit: int = 20) -> list[dict[str, Any]]:
        """Shopify cannot search transactions by amount, so this scans the 100 most recent orders.
        Enough for a demo store; a large store would keep its own index of charges."""
        q = f"""query {{ orders(first: 100, sortKey: PROCESSED_AT, reverse: true) {{
              nodes {{ name {PAYMENT_FIELDS} }} }} }}"""
        hits = []
        for node in self.client.graphql(q)["orders"]["nodes"]:
            for t in self._transactions(node):
                if t["kind"] == "sale" and Decimal(t["amount"]) == amount:
                    hits.append({**t, "order_name": node["name"]})
        hits.sort(key=lambda t: t["created_at"], reverse=True)
        return hits[:limit]

    # Refund proposals, kept on the order

    def _load_proposals(self, order_id: int) -> list[dict[str, Any]]:
        field = self._payments(order_id).get("metafield")
        return json.loads(field["value"]) if field and field.get("value") else []

    def _save_proposals(self, order_id: int, proposals: list[dict[str, Any]]) -> None:
        self.client.mutate(
            """mutation($m: [MetafieldsSetInput!]!) {
                 metafieldsSet(metafields: $m) { metafields { id } userErrors { field message } }
               }""",
            {
                "m": [
                    {
                        "ownerId": gid("Order", order_id),
                        "namespace": PROPOSALS_NAMESPACE,
                        "key": PROPOSALS_KEY,
                        "type": "json",
                        "value": json.dumps(proposals),
                    }
                ]
            },
            "metafieldsSet",
        )

    def file_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        order_id = int(proposal["order_id"])
        existing = self._load_proposals(order_id)
        filed = {
            **proposal,
            "id": order_id * 1000 + len(existing) + 1,
            "status": "pending_approval",
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self._save_proposals(order_id, [*existing, filed])
        return filed

    def proposals(self, order_id: int | None = None) -> list[dict[str, Any]]:
        if order_id is None:
            raise NotImplementedError("LiveShopify lists proposals per order")
        return self._load_proposals(order_id)

    def close_proposal(self, proposal_id: int, status: str) -> None:
        order_id = proposal_id // 1000
        proposals = self._load_proposals(order_id)
        for p in proposals:
            if p["id"] == proposal_id:
                p["status"] = status
        self._save_proposals(order_id, proposals)

    # Money: called only by the approval app, after a person approves and the refund is re-checked

    def issue_refund(
        self, order_name: str, amount: Decimal, note: str, proposal_id: int | None = None
    ) -> dict[str, Any]:
        node = self._order_node(order_name)
        order_id = int(node["legacyResourceId"])
        payments = self._payments(order_id)
        sales = [
            t
            for t in payments.get("transactions") or []
            if t["kind"] in ("SALE", "CAPTURE") and t["status"] == "SUCCESS"
        ]
        if not sales:
            raise ShopifyError(f"Order {order_key(order_name)} has no successful payment to refund")
        parent = sales[-1]  # for a duplicate capture, the second one
        key = str(uuid.uuid4())
        result = self.client.mutate(
            f"""mutation($input: RefundInput!) {{
                 refundCreate(input: $input) @idempotent(key: "{key}") {{
                   refund {{ id legacyResourceId createdAt note totalRefundedSet {{ shopMoney {{ amount }} }} }}
                   userErrors {{ field message }}
                 }}
               }}""",
            {
                "input": {
                    "orderId": node["id"],
                    "note": note,
                    "notify": False,
                    "transactions": [
                        {
                            "orderId": node["id"],
                            "parentId": parent["id"],
                            "kind": "REFUND",
                            "gateway": parent["gateway"],
                            "amount": str(amount.quantize(Decimal("0.01"))),
                        }
                    ],
                }
            },
            "refundCreate",
        )
        r = result["refund"]
        if proposal_id is not None:
            self.close_proposal(proposal_id, "approved")
        return {
            "id": int(r["legacyResourceId"]),
            "order_id": order_id,
            "transaction_id": legacy(parent["id"]),
            "amount": _amount(r["totalRefundedSet"]),
            "note": r.get("note") or note,
            "created_at": _ts(r["createdAt"]),
        }
