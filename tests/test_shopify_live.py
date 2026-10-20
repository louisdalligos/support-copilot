"""The live Shopify backend, against a stand-in for Shopify's API (no network)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from copilot.shopify.backend import FakeShopify, NotFound, connect
from copilot.shopify.demo_store import US_CITIES, DemoStore, Shift, demo_address, demo_data
from copilot.shopify.live import AdminClient, LiveShopify, ShopifyError, _readable, gid, legacy, store_domain

ORDER = {
    "id": "gid://shopify/Order/5551234",
    "legacyResourceId": "5551234",
    "name": "#1804",
    "email": "Ana.Lee@example.com",
    "createdAt": "2026-10-20T01:00:00Z",
    "processedAt": "2026-10-01T10:00:00Z",
    "currencyCode": "USD",
    "tags": ["northwind-demo"],
    "displayFinancialStatus": "PAID",
    "displayFulfillmentStatus": "FULFILLED",
    "totalPriceSet": {"shopMoney": {"amount": "28.95"}},
    "subtotalPriceSet": {"shopMoney": {"amount": "22.0"}},
    "totalShippingPriceSet": {"shopMoney": {"amount": "6.95"}},
    "customer": {"legacyResourceId": "777"},
    "shippingAddress": {"countryCodeV2": "US"},
    "lineItems": {
        "nodes": [
            {
                "id": "gid://shopify/LineItem/42",
                "title": "Kenya Nyeri 12 oz",
                "quantity": 1,
                "originalUnitPriceSet": {"shopMoney": {"amount": "22.00"}},
                "product": {"legacyResourceId": "9", "productType": "coffee", "tags": ["northwind-demo"]},
            },
            {
                "id": "gid://shopify/LineItem/43",
                "title": "Mug",
                "quantity": 1,
                "originalUnitPriceSet": {"shopMoney": {"amount": "12.00"}},
                "product": {"legacyResourceId": "10", "productType": "merch", "tags": ["Final-Sale"]},
            },
        ]
    },
    "fulfillments": [
        {
            "createdAt": "2026-10-20T01:00:01Z",
            "deliveredAt": "2026-10-20T01:00:02Z",
            "inTransitAt": None,
            "events": {
                "nodes": [
                    {"status": "IN_TRANSIT", "happenedAt": "2026-10-02T09:00:00Z"},
                    {"status": "DELIVERED", "happenedAt": "2026-10-05T15:00:00Z"},
                ]
            },
        }
    ],
}

PAYMENTS = {
    "legacyResourceId": "5551234",
    "createdAt": "2026-10-20T01:00:00Z",
    "processedAt": "2026-10-01T10:00:00Z",
    "tags": ["northwind-demo"],
    "transactions": [
        {
            "id": "gid://shopify/OrderTransaction/1",
            "kind": "SALE",
            "status": "SUCCESS",
            "gateway": "bogus",
            "createdAt": "2026-10-20T01:00:00Z",
            "processedAt": "2026-10-01T10:00:05Z",
            "amountSet": {"shopMoney": {"amount": "28.95"}},
            "parentTransaction": None,
            "paymentDetails": {"number": "•••• •••• •••• 4242"},
        },
        {
            "id": "gid://shopify/OrderTransaction/2",
            "kind": "CAPTURE",
            "status": "SUCCESS",
            "gateway": "bogus",
            "createdAt": "2026-10-20T01:00:00Z",
            "processedAt": None,
            "amountSet": {"shopMoney": {"amount": "28.95"}},
            "parentTransaction": None,
            "paymentDetails": None,
        },
    ],
    "refunds": [
        {
            "id": "gid://shopify/Refund/88",
            "legacyResourceId": "88",
            "createdAt": "2026-10-03T12:00:00Z",
            "note": None,
            "totalRefundedSet": {"shopMoney": {"amount": "5"}},
            "transactions": {"nodes": [{"parentTransaction": {"id": "gid://shopify/OrderTransaction/1"}}]},
        }
    ],
}


class FakeAPI:
    """Answers Shopify's token endpoint and the GraphQL queries the backend sends."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.tokens = 0
        self.metafield: str | None = None
        self.reject_next = False
        self.user_errors: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/oauth/access_token":
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "expires_in": 86399})
        body = json.loads(request.content)
        self.calls.append({**body, "token": request.headers.get("X-Shopify-Access-Token")})
        if self.reject_next:
            self.reject_next = False
            return httpx.Response(401, json={"errors": "invalid token"})
        q = body["query"]
        if "metafieldsSet" in q:
            self.metafield = body["variables"]["m"][0]["value"]
            return httpx.Response(200, json={"data": {"metafieldsSet": {"metafields": [], "userErrors": []}}})
        if "refundCreate" in q:
            refund = {
                "id": "gid://shopify/Refund/99",
                "legacyResourceId": "99",
                "createdAt": "2026-10-20T02:00:00Z",
                "note": body["variables"]["input"]["note"],
                "totalRefundedSet": {
                    "shopMoney": {"amount": body["variables"]["input"]["transactions"][0]["amount"]}
                },
            }
            payload = {"refund": None if self.user_errors else refund, "userErrors": self.user_errors}
            return httpx.Response(200, json={"data": {"refundCreate": payload}})
        if "order(id:" in q:
            field = {"value": self.metafield} if self.metafield else None
            return httpx.Response(200, json={"data": {"order": {**PAYMENTS, "metafield": field}}})
        if "orders(" in q:
            return httpx.Response(200, json={"data": {"orders": {"nodes": [ORDER]}}})
        if "customers(" in q:
            node = {
                "legacyResourceId": "777",
                "email": "ana.lee@example.com",
                "firstName": "Ana",
                "lastName": "Lee",
                "createdAt": "2025-01-02T03:04:05Z",
                "defaultAddress": {"countryCodeV2": "US", "provinceCode": "OR"},
                "emailMarketingConsent": {"marketingState": "SUBSCRIBED"},
            }
            return httpx.Response(200, json={"data": {"customers": {"nodes": [node]}}})
        return httpx.Response(200, json={"errors": [{"message": f"unexpected query: {q[:60]}"}]})


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def shop(api: FakeAPI) -> LiveShopify:
    client = AdminClient("demo-store", "id", "secret", http=httpx.Client(transport=httpx.MockTransport(api)))
    return LiveShopify(client)


def test_gids_round_trip():
    assert legacy(gid("Order", 12)) == 12 and legacy(None) is None


def test_signs_in_once_and_again_after_a_401(shop, api):
    shop.order("1804")
    shop.order("#1804")
    assert api.tokens == 1 and {c["token"] for c in api.calls} == {"tok1"}
    api.reject_next = True
    shop.order("#1804")
    assert api.tokens == 2 and api.calls[-1]["token"] == "tok2"


def test_order_has_the_seed_datas_shape(shop):
    o = shop.order("#1804")
    assert o["id"] == 5551234 and o["name"] == "#1804" and o["customer_id"] == 777
    assert o["created_at"] == "2026-10-01T10:00:00Z"  # processedAt: when the order was placed
    assert o["shipped_at"] == "2026-10-02" and o["delivered_at"] == "2026-10-05"  # from the events, not "now"
    assert (o["total"], o["subtotal"], o["shipping"]) == ("28.95", "22.00", "6.95")
    assert o["financial_status"] == "paid" and o["fulfillment_status"] == "fulfilled"
    coffee, mug = o["line_items"]
    assert coffee == {
        "id": 42,
        "title": "Kenya Nyeri 12 oz",
        "quantity": 1,
        "price": "22.00",
        "product_id": 9,
        "product_type": "coffee",
        "final_sale": False,
    }
    assert mug["final_sale"] is True


def test_missing_order(shop):
    with pytest.raises(NotFound):
        shop.order("#9999")  # the stand-in only knows #1804


def test_payments(shop):
    txs = shop.transactions(5551234)
    assert [t["kind"] for t in txs] == ["sale", "sale"]  # a capture is money taken, like a sale
    assert txs[0]["card_last4"] == "4242" and txs[1]["card_last4"] is None
    assert txs[0]["created_at"] == "2026-10-01T10:00:05Z"
    [refund] = shop.refunds(5551234)
    assert refund == {
        "id": 88,
        "order_id": 5551234,
        "transaction_id": 1,
        "amount": "5.00",
        "note": "",
        "created_at": "2026-10-03T12:00:00Z",
    }


def test_customer(shop):
    c = shop.customer("ANA.LEE@example.com ")
    assert c["id"] == 777 and c["province"] == "OR" and c["accepts_marketing"] is True


def test_proposals_live_on_the_order(shop, api):
    filed = shop.file_proposal({"order_id": 5551234, "order_name": "#1804", "amount": "22.00"})
    assert filed["id"] == 5551234001 and filed["status"] == "pending_approval"
    assert json.loads(api.metafield)[0]["amount"] == "22.00"
    second = shop.file_proposal({"order_id": 5551234, "order_name": "#1804", "amount": "5.00"})
    assert second["id"] == 5551234002
    shop.close_proposal(second["id"], "rejected")
    assert [p["status"] for p in shop.proposals(5551234)] == ["pending_approval", "rejected"]


def test_refund_goes_to_the_last_payment_and_is_idempotent(shop, api):
    filed = shop.file_proposal({"order_id": 5551234, "order_name": "#1804", "amount": "22.00"})
    refund = shop.issue_refund("#1804", Decimal("22"), "Approved by reviewer", proposal_id=filed["id"])
    call = next(c for c in api.calls if "refundCreate" in c["query"])
    assert "@idempotent(key:" in call["query"]
    sent = call["variables"]["input"]
    assert sent["notify"] is False and sent["orderId"] == ORDER["id"]
    assert sent["transactions"][0] == {
        "orderId": ORDER["id"],
        "parentId": "gid://shopify/OrderTransaction/2",
        "kind": "REFUND",
        "gateway": "bogus",
        "amount": "22.00",
    }
    assert refund["id"] == 99 and refund["amount"] == "22.00" and refund["transaction_id"] == 2
    assert shop.proposals(5551234)[0]["status"] == "approved"


def test_user_errors_are_raised(shop, api):
    api.user_errors = [{"field": ["transactions", "0", "amount"], "message": "is greater than refundable"}]
    with pytest.raises(ShopifyError, match="greater than refundable"):
        shop.issue_refund("#1804", Decimal("500"), "x")


def test_connect_picks_the_backend(monkeypatch, tmp_path):
    from pathlib import Path

    data = Path(__file__).resolve().parents[1] / "data" / "generated"
    monkeypatch.setenv("COPILOT_SHOPIFY", "fake")
    assert isinstance(connect(data), FakeShopify)
    monkeypatch.setenv("COPILOT_SHOPIFY", "live")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "demo-store.myshopify.com")
    monkeypatch.setenv("SHOPIFY_CLIENT_ID", "id")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "secret")
    backend = connect(data)
    assert isinstance(backend, LiveShopify) and backend.client.shop == "demo-store.myshopify.com"
    monkeypatch.setenv("COPILOT_SHOPIFY", "fake")


def test_live_store_uses_the_real_date(monkeypatch):
    from copilot.store import TODAY, today

    monkeypatch.setenv("COPILOT_SHOPIFY", "fake")
    monkeypatch.setenv("COPILOT_CLOCK", "fixed")
    assert today() == TODAY
    monkeypatch.setenv("COPILOT_SHOPIFY", "live")
    assert today() == date.today()
    monkeypatch.setenv("COPILOT_SHOPIFY", "fake")


def test_demo_dates_keep_their_age():
    shift = Shift(today=date(2026, 11, 20))  # 36 days after the seed data's fixed date
    assert shift.days == 36
    assert shift.ts("2026-10-15T07:11:00Z") == "2026-11-20T07:11:00Z"
    assert shift.day("2026-10-05") == "2026-11-10T15:00:00Z"


def test_demo_orders_cover_the_demo_tickets():
    data = demo_data()
    names = {o["name"] for o in data["orders"]}
    assert "#1792" in names  # ticket 10301, the duplicate charge
    titles = {p["title"] for p in data["products"]}
    assert all(li["title"] in titles for o in data["orders"] for li in o["line_items"])


def test_demo_order_payload():
    data = demo_data()
    o = next(o for o in data["orders"] if o["name"] == "#1792")
    txs = [t for t in data["transactions"] if t["order_id"] == o["id"]]
    customer = data["customers"][o["email"].lower()]
    variants = {li["title"]: "gid://shopify/ProductVariant/1" for li in o["line_items"]}
    store = DemoStore(client=None, shift=Shift(today=date(2026, 10, 15)))  # type: ignore[arg-type]
    payload = store.order_input(o, customer, variants, txs, "gid://shopify/Location/1")
    assert payload["name"] == "#1792" and payload["test"] is True and "northwind-demo" in payload["tags"]
    assert [t["status"] for t in payload["transactions"]] == ["SUCCESS", "SUCCESS"]  # captured twice
    assert all(t["gateway"] == "bogus" and t["test"] for t in payload["transactions"])
    assert payload["processedAt"] == o["created_at"]  # no shift when today is the seed date
    assert payload["fulfillment"]["notifyCustomer"] is False


def test_store_domain_accepts_what_people_paste():
    for value in (
        "demo",
        "demo.myshopify.com",
        "https://demo.myshopify.com/",
        " admin.shopify.com/store/demo/orders ",
    ):
        assert store_domain(value) == "demo.myshopify.com"


def test_html_errors_are_readable():
    page = "<!DOCTYPE html><html><head><title>Oauth error</title><style>p{}</style></head><body><p>invalid_request: The client_id is invalid</p></body></html>"
    assert _readable(page) == "Oauth error: Oauth error invalid_request: The client_id is invalid"


def test_setup_waits_when_shopify_limits_new_orders():
    class Limited:
        calls = 0

        def mutate(self, query, variables, field):
            Limited.calls += 1
            if Limited.calls < 3:
                raise ShopifyError("orderCreate: Too many attempts. Please try again later.")
            return {"order": {"id": "gid://shopify/Order/1", "name": "#1"}}

    waits = []
    store = DemoStore(client=Limited(), sleep=waits.append)  # type: ignore[arg-type]
    assert store.create_order({"name": "#1"}, log=lambda *_: None)["name"] == "#1"
    assert len(waits) == 2


def test_setup_does_not_retry_other_errors():
    class Broken:
        def mutate(self, query, variables, field):
            raise ShopifyError("orderCreate: name is invalid")

    store = DemoStore(client=Broken(), sleep=lambda s: None)  # type: ignore[arg-type]
    with pytest.raises(ShopifyError, match="name is invalid"):
        store.create_order({"name": "#1"}, log=lambda *_: None)


def test_demo_addresses_match_the_state():
    assert demo_address("US", "NC") == {
        "address1": "1 Demo Street",
        "city": "Raleigh",
        "zip": "27601",
        "provinceCode": "NC",
        "countryCode": "US",
    }
    assert demo_address("US", None)["provinceCode"] == "OR"  # unknown state: a consistent default
    states = {c.get("province") for c in demo_data()["customers"].values()}
    assert states <= set(US_CITIES)  # every demo customer's state has a real ZIP code
