"""Fill a Shopify development store with the Northwind demo data.

    python -m copilot.shopify.demo_store setup      # create what is missing (safe to run again)
    python -m copilot.shopify.demo_store status     # what is there

It creates the orders that the labeled demo tickets (10301-10352) refer to, with
their customers and products, under the same order names (#1792...), so those
tickets work against the real store. Dates move forward by the days between the
seed data's fixed date (2026-10-15) and today, so every order keeps its age: a
parcel delivered 10 days before the ticket was written is delivered 10 days ago.

Payments use Shopify's test gateway ("bogus"); orders are marked as test orders.
Deliveries are recorded as fulfillment events on the original dates, and past
refunds are created with their original (shifted) dates.

Needs SHOPIFY_STORE_DOMAIN, SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET (see docs/shopify.md).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from copilot.shopify.live import FINAL_SALE_TAG, AdminClient, ShopifyError
from copilot.store import STORE_NAME, TODAY

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data" / "generated"
DEMO_TAG = "northwind-demo"
GATEWAY = "bogus"
RATE_WAIT = 30  # seconds to wait when Shopify limits order creation


def _rows(name: str) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in (DATA / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if line
    ]


def demo_data() -> dict[str, Any]:
    """The orders the demo tickets need: every order of every customer who wrote one."""
    tickets = _rows("eval_tickets")
    emails = {t["ticket"]["requester_email"].lower() for t in tickets}
    orders = [o for o in _rows("orders") if o["email"].lower() in emails]
    ids = {o["id"] for o in orders}
    product_ids = {li["product_id"] for o in orders for li in o["line_items"]}
    customers = {c["email"].lower(): c for c in _rows("customers") if c["email"].lower() in emails}
    return {
        "orders": sorted(orders, key=lambda o: o["created_at"]),
        "customers": customers,
        "products": [p for p in _rows("products") if p["id"] in product_ids],
        "transactions": [t for t in _rows("transactions") if t["order_id"] in ids],
        "refunds": [r for r in _rows("refunds") if r["order_id"] in ids],
    }


# A real city and ZIP code for each state, so Shopify does not flag the demo addresses.
US_CITIES = {
    "AZ": ("Phoenix", "85004"),
    "CA": ("San Francisco", "94103"),
    "CO": ("Denver", "80202"),
    "FL": ("Miami", "33130"),
    "GA": ("Atlanta", "30303"),
    "IL": ("Chicago", "60601"),
    "MA": ("Boston", "02108"),
    "MI": ("Detroit", "48226"),
    "MN": ("Minneapolis", "55401"),
    "NC": ("Raleigh", "27601"),
    "NJ": ("Newark", "07102"),
    "NY": ("New York", "10001"),
    "OH": ("Columbus", "43215"),
    "OR": ("Portland", "97201"),
    "PA": ("Philadelphia", "19103"),
    "TN": ("Nashville", "37203"),
    "TX": ("Austin", "78701"),
    "VA": ("Richmond", "23219"),
    "WA": ("Seattle", "98101"),
}
OTHER_COUNTRIES = {"CA": ("Toronto", "M5H 2N2", "ON"), "GB": ("London", "SW1A 1AA", None)}


def demo_address(country: str, province: str | None) -> dict[str, Any]:
    """A plausible shipping address in the customer's state (or country)."""
    if country == "US":
        state = province if province in US_CITIES else "OR"
        city, zip_code = US_CITIES[state]
        return {
            "address1": "1 Demo Street",
            "city": city,
            "zip": zip_code,
            "provinceCode": state,
            "countryCode": "US",
        }
    city, zip_code, region = OTHER_COUNTRIES.get(country, ("Demo City", "", None))
    address = {"address1": "1 Demo Street", "city": city, "zip": zip_code, "countryCode": country}
    if region:
        address["provinceCode"] = region
    return address


class Shift:
    """Moves the seed data's dates so that its fixed 'today' becomes the real today."""

    def __init__(self, today: date | None = None) -> None:
        self.days = ((today or date.today()) - TODAY).days

    def ts(self, value: str) -> str:
        moved = datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(days=self.days)
        return moved.strftime("%Y-%m-%dT%H:%M:%SZ")

    def day(self, value: str, hour: int = 15) -> str:
        return (date.fromisoformat(value) + timedelta(days=self.days)).isoformat() + f"T{hour:02d}:00:00Z"


def money(amount: str) -> dict[str, Any]:
    return {"shopMoney": {"amount": amount, "currencyCode": "USD"}}


class DemoStore:
    def __init__(self, client: AdminClient, shift: Shift | None = None, sleep: Any = time.sleep) -> None:
        self.client = client
        self.shift = shift or Shift()
        self.sleep = sleep

    # Products

    def existing_products(self) -> dict[str, str]:
        """Demo products already in the store: title -> variant id."""
        q = """query($q: String!) { products(first: 250, query: $q) {
                 nodes { title variants(first: 1) { nodes { id } } } } }"""
        nodes = self.client.graphql(q, {"q": f"tag:{DEMO_TAG}"})["products"]["nodes"]
        return {n["title"]: n["variants"]["nodes"][0]["id"] for n in nodes if n["variants"]["nodes"]}

    def create_product(self, p: dict[str, Any]) -> str:
        tags = [DEMO_TAG, p["product_type"]] + ([FINAL_SALE_TAG] if p.get("final_sale") else [])
        created = self.client.mutate(
            """mutation($p: ProductCreateInput!) {
                 productCreate(product: $p) {
                   product { id variants(first: 1) { nodes { id } } }
                   userErrors { field message }
                 }
               }""",
            {
                "p": {
                    "title": p["title"],
                    "productType": p["product_type"],
                    "vendor": STORE_NAME,
                    "tags": tags,
                }
            },
            "productCreate",
        )["product"]
        variant = created["variants"]["nodes"][0]["id"]
        self.client.mutate(
            """mutation($id: ID!, $v: [ProductVariantsBulkInput!]!) {
                 productVariantsBulkUpdate(productId: $id, variants: $v) {
                   productVariants { id }
                   userErrors { field message }
                 }
               }""",
            {
                "id": created["id"],
                "v": [{"id": variant, "price": p["price"], "inventoryItem": {"sku": p["sku"]}}],
            },
            "productVariantsBulkUpdate",
        )
        return variant

    # Orders

    def existing_orders(self) -> set[str]:
        q = """query($q: String!, $after: String) { orders(first: 250, query: $q, after: $after) {
                 nodes { name } pageInfo { hasNextPage endCursor } } }"""
        names: set[str] = set()
        after = None
        while True:
            page = self.client.graphql(q, {"q": f"tag:{DEMO_TAG}", "after": after})["orders"]
            names |= {n["name"] for n in page["nodes"]}
            if not page["pageInfo"]["hasNextPage"]:
                return names
            after = page["pageInfo"]["endCursor"]

    def location(self) -> str:
        nodes = self.client.graphql("{ locations(first: 1) { nodes { id } } }")["locations"]["nodes"]
        if not nodes:
            raise ShopifyError("The store has no location to ship from")
        return nodes[0]["id"]

    def order_input(
        self,
        o: dict[str, Any],
        customer: dict[str, Any],
        variants: dict[str, str],
        txs: list[dict[str, Any]],
        location: str,
    ) -> dict[str, Any]:
        address = {
            "firstName": customer["first_name"],
            "lastName": customer["last_name"],
            **demo_address(o.get("shipping_country") or "US", customer.get("province")),
        }
        order: dict[str, Any] = {
            "name": o["name"],
            "email": o["email"],
            "processedAt": self.shift.ts(o["created_at"]),
            "currency": "USD",
            "test": True,
            "tags": [DEMO_TAG, *o.get("tags", [])],
            "customer": {
                "toUpsert": {
                    "email": o["email"],
                    "firstName": customer["first_name"],
                    "lastName": customer["last_name"],
                }
            },
            "shippingAddress": address,
            "lineItems": [
                {
                    "variantId": variants[li["title"]],
                    "quantity": li["quantity"],
                    "priceSet": money(li["price"]),
                }
                for li in o["line_items"]
            ],
            "transactions": [
                {
                    "kind": "SALE",
                    "status": "SUCCESS" if t["status"] == "success" else "FAILURE",
                    "gateway": GATEWAY,
                    "test": True,
                    "amountSet": money(t["amount"]),
                    "processedAt": self.shift.ts(t["created_at"]),
                }
                for t in txs
                if t["kind"] == "sale"
            ],
        }
        if Decimal(o.get("shipping") or "0") > 0:
            order["shippingLines"] = [{"title": "Standard shipping", "priceSet": money(o["shipping"])}]
        if o.get("fulfillment_status") == "fulfilled":
            order["fulfillment"] = {
                "locationId": location,
                "notifyCustomer": False,
                "trackingCompany": "USPS",
                "trackingNumber": f"9400{o['id']:012d}",
            }
        return order

    def create_order(self, payload: dict[str, Any], log: Any = print) -> dict[str, Any]:
        """Development stores limit how fast orders can be created ("Too many attempts"), so wait
        and try again, for up to about ten minutes."""
        for attempt in range(20):
            try:
                return self._create_order(payload)
            except ShopifyError as e:
                if "too many attempts" not in str(e).lower() or attempt == 19:
                    raise
                log(f"         Shopify is limiting new orders; waiting {RATE_WAIT}s before {payload['name']}")
                self.sleep(RATE_WAIT)
        raise AssertionError("unreachable")

    def _create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.client.mutate(
            """mutation($o: OrderCreateOrderInput!, $opt: OrderCreateOptionsInput) {
                 orderCreate(order: $o, options: $opt) {
                   order {
                     id name
                     fulfillments(first: 5) { id }
                     transactions(first: 10) { id kind status amountSet { shopMoney { amount } } }
                   }
                   userErrors { field message }
                 }
               }""",
            {"o": payload, "opt": {"sendReceipt": False, "sendFulfillmentReceipt": False}},
            "orderCreate",
        )["order"]

    def add_event(self, fulfillment_id: str, status: str, when: str) -> None:
        self.client.mutate(
            """mutation($e: FulfillmentEventInput!) {
                 fulfillmentEventCreate(fulfillmentEvent: $e) {
                   fulfillmentEvent { id }
                   userErrors { field message }
                 }
               }""",
            {"e": {"fulfillmentId": fulfillment_id, "status": status, "happenedAt": when}},
            "fulfillmentEventCreate",
        )

    def add_refund(self, order: dict[str, Any], parent: dict[str, Any], r: dict[str, Any]) -> None:
        self.client.mutate(
            f"""mutation($input: RefundInput!) {{
                 refundCreate(input: $input) @idempotent(key: "{uuid.uuid4()}") {{
                   refund {{ id }}
                   userErrors {{ field message }}
                 }}
               }}""",
            {
                "input": {
                    "orderId": order["id"],
                    "note": r.get("note") or "Customer request",
                    "notify": False,
                    "processedAt": self.shift.ts(r["created_at"]),
                    "transactions": [
                        {
                            "orderId": order["id"],
                            "parentId": parent["id"],
                            "kind": "REFUND",
                            "gateway": GATEWAY,
                            "amount": r["amount"],
                        }
                    ],
                }
            },
            "refundCreate",
        )

    def setup(self, log: Any = print) -> dict[str, int]:
        data = demo_data()
        made = {"products": 0, "orders": 0, "skipped": 0}

        variants = self.existing_products()
        for p in data["products"]:
            if p["title"] not in variants:
                variants[p["title"]] = self.create_product(p)
                made["products"] += 1
                log(f"product  {p['title']}")

        have = self.existing_orders()
        location = self.location()
        for o in data["orders"]:
            if o["name"] in have:
                made["skipped"] += 1
                continue
            txs = sorted(
                (t for t in data["transactions"] if t["order_id"] == o["id"]), key=lambda t: t["created_at"]
            )
            customer = data["customers"][o["email"].lower()]
            created = self.create_order(self.order_input(o, customer, variants, txs, location), log)
            for f in created.get("fulfillments") or []:
                if o.get("shipped_at"):
                    self.add_event(f["id"], "IN_TRANSIT", self.shift.day(o["shipped_at"], 9))
                if o.get("delivered_at"):
                    self.add_event(f["id"], "DELIVERED", self.shift.day(o["delivered_at"], 15))
            successes = [t for t in created.get("transactions") or [] if t["status"] == "SUCCESS"]
            for r in sorted(
                (r for r in data["refunds"] if r["order_id"] == o["id"]), key=lambda r: r["created_at"]
            ):
                if successes:
                    self.add_refund(created, successes[-1], r)
            made["orders"] += 1
            log(f"order    {o['name']}  {o['email']}")
        return made

    def status(self) -> dict[str, int]:
        data = demo_data()
        have = self.existing_orders()
        return {
            "demo_orders_expected": len(data["orders"]),
            "demo_orders_in_store": len(have & {o["name"] for o in data["orders"]}),
            "demo_products_in_store": len(self.existing_products()),
            "date_shift_days": self.shift.days,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m copilot.shopify.demo_store",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=["setup", "status"])
    args = parser.parse_args(argv)

    from copilot.agent.__main__ import load_dotenv

    load_dotenv()
    store = DemoStore(AdminClient.from_env())
    try:
        result = store.setup() if args.command == "setup" else store.status()
    except ShopifyError as e:
        print(f"Shopify said: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
