"""Synthetic customers. Every email uses the reserved example.com domain."""

from __future__ import annotations

import random

from copilot.seed.common import Dataset, add_days, iso_dt
from copilot.store import INTERNATIONAL_COUNTRIES, TODAY

FIRST_NAMES = [
    "Ava", "Ben", "Carmen", "Dmitri", "Elena", "Farah", "Gabriel", "Hana", "Isaac", "Jade",
    "Kofi", "Lena", "Marco", "Nadia", "Omar", "Priya", "Quinn", "Rosa", "Samuel", "Tara",
    "Umar", "Vera", "Wes", "Ximena", "Yusuf", "Zoe", "Alma", "Bruno", "Celia", "Diego",
    "Esther", "Felix", "Grace", "Hugo", "Iris", "Jonah", "Kira", "Leo", "Maya", "Noah",
]  # fmt: skip
LAST_NAMES = [
    "Abbott", "Bautista", "Chen", "Diaz", "Eriksen", "Fischer", "Garcia", "Haddad", "Ito",
    "Johansson", "Kowalski", "Lopez", "Mensah", "Nakamura", "Okafor", "Patel", "Quintero",
    "Reyes", "Silva", "Tanaka", "Ueda", "Varga", "Walsh", "Xu", "Yilmaz", "Zimmerman",
    "Baker", "Castillo", "Dubois", "Evans", "Moreau", "Novak", "Santos", "Torres",
]  # fmt: skip
US_STATES = ["OR", "WA", "CA", "NY", "TX", "IL", "CO", "MA", "FL", "GA", "MN", "AZ", "NC", "PA"]

CUSTOMER_COUNT = 300


def new_customer(ds: Dataset, rng: random.Random, *, country: str = "US") -> dict:
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    cid = ds.ids.next("customer")
    customer = {
        "id": cid,
        "first_name": first,
        "last_name": last,
        "email": f"{first}.{last}.{cid}@example.com".lower(),
        "country": country,
        "province": rng.choice(US_STATES) if country == "US" else None,
        "created_at": iso_dt(add_days(TODAY, -rng.randint(30, 900)), rng),
        "accepts_marketing": rng.random() < 0.4,
    }
    ds.customers.append(customer)
    return customer


def build_customers(ds: Dataset, rng: random.Random) -> None:
    for _ in range(CUSTOMER_COUNT):
        country = "US" if rng.random() < 0.9 else rng.choice(INTERNATIONAL_COUNTRIES)
        new_customer(ds, rng, country=country)
