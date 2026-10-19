"""Facts about the fictional store, Northwind Coffee Co.

This module is the single source of truth for the numbers the seed generator,
the policy checks and the eval labels depend on. The knowledge-base documents
in data/kb state the same facts in prose; tests/test_kb.py checks they agree.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

STORE_NAME = "Northwind Coffee Co."
SUPPORT_EMAIL = "support@northwind-coffee.example"

# A fixed "today" so refund windows, ticket ages and eval labels never drift.
TODAY = date(2026, 10, 15)

# The current policies took effect on this date; earlier orders used the v1 policies.
POLICY_V2_DATE = date(2026, 3, 1)

# Refunds (data/kb/refund-policy.md)
REFUND_WINDOW_DAYS = 30
DAMAGE_REPORT_DAYS = 14
DEFECTIVE_COFFEE_DAYS = 30
WARRANTY_DAYS = 365

# Previous refund policy (data/kb/refund-policy-v1.md), kept to test stale-document handling.
V1_REFUND_WINDOW_DAYS = 14

# Shipping (data/kb/shipping-policy.md, international-shipping.md)
FREE_SHIPPING_THRESHOLD = Decimal("50.00")
STANDARD_SHIPPING = Decimal("6.95")
EXPRESS_SHIPPING = Decimal("14.95")
INTERNATIONAL_SHIPPING = Decimal("19.95")
V1_FREE_SHIPPING_THRESHOLD = Decimal("35.00")
V1_STANDARD_SHIPPING = Decimal("5.95")

# Payments (data/kb/payment-methods.md, statement-descriptor.md)
DESCRIPTOR_ORDER = "NWC*NORTHWIND COFFEE"
DESCRIPTOR_SUBSCRIPTION = "NWC*COFFEE SUBSCRIPTION"
REFUND_PROCESSING = "5 to 10 business days"
AUTH_HOLD_RELEASE = "3 to 7 business days"

SUBSCRIPTION_DISCOUNT = Decimal("0.10")
PRICE_ADJUSTMENT_DAYS = 7

INTERNATIONAL_COUNTRIES = ("CA", "GB", "DE", "FR", "NL", "IE", "AU")
