"""Product catalog: coffee, equipment, merch, subscriptions and gift cards."""

from __future__ import annotations

from decimal import Decimal

from copilot.seed.common import Dataset, fmt_money, money

SINGLE_ORIGINS = [
    ("Ethiopia Yirgacheffe", ("light",), "21.00"),
    ("Kenya Nyeri", ("light",), "22.00"),
    ("Colombia Huila", ("medium", "dark"), "18.00"),
    ("Guatemala Antigua", ("medium",), "19.00"),
    ("Brazil Cerrado", ("medium", "dark"), "17.00"),
    ("Costa Rica Tarrazu", ("light", "medium"), "20.00"),
    ("Sumatra Mandheling", ("dark",), "19.00"),
    ("Rwanda Huye", ("light",), "21.00"),
    ("Peru Cajamarca", ("medium",), "18.00"),
    ("Honduras Marcala", ("medium",), "18.00"),
    ("Mexico Chiapas", ("medium", "dark"), "17.00"),
    ("Papua New Guinea Waghi", ("medium",), "19.00"),
    ("Burundi Kayanza", ("light",), "21.00"),
    ("El Salvador Santa Ana", ("medium",), "19.00"),
    ("Nicaragua Jinotega", ("medium",), "18.00"),
    ("Panama Boquete", ("light",), "24.00"),
]
BLENDS = [
    ("House Blend", "medium"),
    ("Morning Light", "light"),
    ("Dark Harbor", "dark"),
    ("Espresso Forte", "dark"),
    ("Breakfast Blend", "medium"),
    ("Fireside", "dark"),
    ("Cold Brew Blend", "medium"),
    ("Holiday Blend", "medium"),
]
DECAFS = ["Decaf Colombia", "Decaf House", "Decaf Espresso", "Decaf Peru"]
FLAVORED = ["Hazelnut", "Vanilla", "Cinnamon", "Caramel", "Chocolate Mint", "Maple Pecan"]

# Bag sizes and their price multiplier on the 12 oz price.
SIZES = [("12 oz", Decimal("1")), ("2 lb", Decimal("2.4")), ("5 lb", Decimal("5.5"))]

EQUIPMENT = [
    ("Northwind Burr Grinder", "149.00"),
    ("Hand Grinder", "59.00"),
    ("Gooseneck Kettle 1L", "79.00"),
    ("Stovetop Gooseneck Kettle", "39.00"),
    ("Pour-Over Dripper", "28.00"),
    ("Glass Carafe 600ml", "24.00"),
    ("French Press 34 oz", "39.00"),
    ("French Press 12 oz", "29.00"),
    ("Home Espresso Machine", "429.00"),
    ("Milk Frother", "49.00"),
    ("Coffee Scale with Timer", "45.00"),
    ("Cold Brew Maker", "35.00"),
    ("AeroPress", "39.95"),
    ("Moka Pot 6-Cup", "34.00"),
    ("Espresso Tamper", "29.00"),
    ("Knock Box", "25.00"),
    ("Paper Filters (100)", "8.00"),
    ("Descaling Solution", "12.00"),
    ("Burr Cleaning Tablets", "14.00"),
    ("Airtight Coffee Canister", "26.00"),
]
MERCH = [
    ("Northwind Mug", "16.00", False),
    ("Travel Tumbler", "28.00", False),
    ("Canvas Tote", "18.00", False),
    ("Logo T-Shirt", "25.00", False),
    ("Enamel Camp Mug (Clearance)", "9.00", True),
    ("Holiday Ornament (Clearance)", "6.00", True),
    ("Tasting Notebook", "12.00", False),
    ("Barista Apron", "32.00", False),
]
SUBSCRIPTIONS = [
    ("Subscription: House Blend 12 oz", "14.40"),
    ("Subscription: Roaster's Choice 12 oz", "18.90"),
    ("Subscription: Espresso Forte 2 lb", "34.56"),
    ("Subscription: Decaf House 12 oz", "15.30"),
    ("Subscription: Single Origin Duo", "36.00"),
    ("Subscription: Colombia Huila 2 lb", "38.88"),
]
GIFT_CARDS = ["25.00", "50.00", "100.00"]


def _add(ds: Dataset, title: str, product_type: str, price: Decimal | str, **extra: object) -> None:
    pid = ds.ids.next("product")
    ds.products.append(
        {
            "id": pid,
            "sku": f"NW-{pid}",
            "title": title,
            "product_type": product_type,
            "price": fmt_money(money(price)),
            "final_sale": bool(extra.pop("final_sale", False)),
            "roast": extra.pop("roast", None),
            "size": extra.pop("size", None),
        }
    )


def build_catalog(ds: Dataset) -> None:
    for name, roasts, base in SINGLE_ORIGINS:
        for roast in roasts:
            label = f"{name} ({roast.title()} Roast)" if len(roasts) > 1 else name
            for size, mult in SIZES:
                _add(ds, f"{label} {size}", "coffee", money(Decimal(base) * mult), roast=roast, size=size)
    for name, roast in BLENDS:
        for size, mult in SIZES:
            _add(ds, f"{name} {size}", "coffee", money(Decimal("16.00") * mult), roast=roast, size=size)
    for name in DECAFS:
        for size, mult in SIZES[:2]:
            _add(ds, f"{name} {size}", "coffee", money(Decimal("17.00") * mult), roast="medium", size=size)
    for name in FLAVORED:
        _add(ds, f"{name} Flavored 12 oz", "coffee", "16.00", roast="medium", size="12 oz")
    for name, price in EQUIPMENT:
        _add(ds, name, "equipment", price)
    for name, price, final_sale in MERCH:
        _add(ds, name, "merch", price, final_sale=final_sale)
    for name, price in SUBSCRIPTIONS:
        _add(ds, name, "subscription", price)
    for amount in GIFT_CARDS:
        _add(ds, f"Gift Card ${amount.split('.')[0]}", "gift_card", amount)
