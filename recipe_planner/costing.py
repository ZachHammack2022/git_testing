"""Rough grocery cost for a recipe or a week, for budget mode.

Prices you've actually seen win: when the product preview finds a price for an ingredient it goes
in the price book. Anything not in the book falls back to a typical price (per pound for meat
and fish, per item for the rest), so every number here is an estimate, not a quote.
"""
from __future__ import annotations

import math

from .grocery import build_shopping_list

# Typical $ per pound for meat and fish, by keyword in the ingredient name.
MEAT_PER_LB = [
    ("salmon", 14.0), ("shrimp", 12.0), ("steak", 14.0), ("lamb", 11.0), ("tuna", 12.0), ("cod", 11.0),
    ("halibut", 16.0), ("tilapia", 8.0), ("fish", 11.0), ("bacon", 8.0), ("sausage", 6.0), ("beef", 8.0),
    ("pork", 5.5), ("turkey", 5.0), ("chicken breast", 5.5), ("chicken", 4.5),
]
MEAT_DEFAULT_PER_LB = 8.0
# Typical $ for one purchase of anything else, by aisle.
CATEGORY_PRICE = {"produce": 2.5, "dairy & eggs": 4.0, "bakery": 4.0, "pantry": 3.0, "frozen": 5.0,
                  "spices": 0.0, "other": 3.0, "meat & seafood": 9.0}


def item_price(item: dict, book: dict[str, float] | None = None) -> float:
    """Estimated cost of buying one shopping-list item."""
    book = book or {}
    if item["key"] in book:
        return book[item["key"]]
    if item.get("category") == "meat & seafood":
        ounces = sum(n["qty"] for n in item.get("needs", []) if n["family"] == "weight")
        if ounces > 0:
            name = item["name"].lower()
            per_lb = next((p for w, p in MEAT_PER_LB if w in name), MEAT_DEFAULT_PER_LB)
            return round(max(math.ceil(ounces / 16 * 4) / 4, 0.5) * per_lb, 2)  # quarter-pound steps
    return CATEGORY_PRICE.get(item.get("category"), 3.0)


def cost_of(meals: list[dict], book: dict[str, float] | None, have: list[str] | None) -> float:
    """Estimated groceries for these meals; things in `have` (pantry, on hand) cost nothing."""
    items = build_shopping_list(meals, have or [])
    return round(sum(item_price(i, book) for i in items if not i["in_pantry"]), 2)


def recipe_costs(recipes: list[dict], servings: int, book: dict[str, float] | None,
                 have: list[str] | None) -> dict[int, float]:
    """What each recipe would cost on its own, for the household size. Used to rank recipes."""
    return {r["id"]: cost_of([{"recipe": r, "servings": servings}], book, have) for r in recipes}
