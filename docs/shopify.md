# Connecting a real Shopify store

By default the copilot reads the seed data. With `COPILOT_SHOPIFY=live` it uses a real Shopify development store instead, through the Admin GraphQL API (`copilot/shopify/live.py`). Nothing else changes: the MCP server, the agent and the approval app see the same data shapes, and a refund approved in the app is issued in Shopify.

## 1. A development store and an app

Development stores are free and do not expire. Shopify's test payment gateway ("bogus") is used, so no real money moves.

1. Create a Shopify Partner account and open the **Dev Dashboard**.
2. **Dev stores** > create a **Dev** store. It must be created here, not from the Shopify admin: the app and the store have to be in the same organization.
3. **Apps** > **Create app** > **Start from Dev Dashboard**. On the app's **Versions** page, set:
   - **App URL:** `https://shopify.dev/apps/default-app-home` (the app has no pages of its own)
   - **Scopes:**
     ```
     read_products,write_products,read_customers,write_customers,read_orders,write_orders,read_fulfillments,write_fulfillments,read_merchant_managed_fulfillment_orders,write_merchant_managed_fulfillment_orders,read_locations
     ```
   Then **Release** the version.
4. On the app's page, **Install app** on the dev store.
5. The app's **Settings** page shows the **Client ID** and **Client secret**.

The server signs in with the client credentials grant: it exchanges the ID and secret for an access token that lasts 24 hours, and renews it before it expires.

## 2. Settings

In `.env` (git-ignored):

```bash
COPILOT_SHOPIFY=live
SHOPIFY_STORE_DOMAIN=your-dev-store.myshopify.com
SHOPIFY_CLIENT_ID=...
SHOPIFY_CLIENT_SECRET=...
```

## 3. Demo data

```bash
python -m copilot.shopify.demo_store setup     # safe to run again; creates only what is missing
python -m copilot.shopify.demo_store status
```

This creates the 14 products and 42 orders that the labeled demo tickets (10301 to 10357) refer to, under the same order numbers, with their customers, payments, deliveries and past refunds. Dates move forward so that each order keeps its age relative to today: an order delivered 10 days before the seed data's date is delivered 10 days ago. The orders are tagged `northwind-demo` and marked as test orders.

With a live store, the store's "today" is the real date (it is fixed at 2026-10-15 with the seed data), so refund windows count from real delivery dates. As weeks pass, the demo orders age too: an order that was inside the 30-day window will fall outside it. That is how a real store behaves.

## 4. Try it

```bash
python -m copilot.agent run 10319 --model oracle     # a refund request on a real order
python -m copilot.approvals --model oracle            # approve it; the refund appears on the order in Shopify
```

## How it maps to Shopify

| Copilot | Shopify |
| --- | --- |
| Order, line items, totals | `orders` query; product type and a `final-sale` tag come from the product |
| Shipped and delivered dates | The fulfillment's `IN_TRANSIT` and `DELIVERED` events |
| Payments | Order transactions (a capture counts as a sale) |
| Past refunds | Order refunds |
| Refund proposals waiting for a person | A JSON metafield on the order (`copilot.refund_proposals`), so they survive restarts and show in the admin |
| Issuing an approved refund | `refundCreate`, with an idempotency key, on the last successful payment |
| Charges matching an amount ("what is this $14.40?") | A scan of the 100 most recent orders; Shopify cannot search transactions by amount |
| Disputes | None: development stores do not use Shopify Payments |
