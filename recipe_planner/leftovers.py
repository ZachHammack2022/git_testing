"""Work out what's left over after a grocery order, and roughly how long it will keep.

You buy a whole bunch of mint for a recipe that uses 1/4 bunch, or a 16 oz tub of sour cream for 3/4 cup.
The remainder goes on your Kitchen list with a use-by date, so the next plan can use it up.
"""
from __future__ import annotations

import datetime as dt
import math

from .grocery import _nice_number, format_amount, normalize_name

# Typical pack sizes for things sold by weight or volume (weight in oz, volume in tsp).
# (keyword in the ingredient name, family, pack size, days it keeps once opened)
PACKS = [
    ("sour cream", "volume", 96, 14), ("heavy cream", "volume", 96, 10), ("half and half", "volume", 96, 10),
    ("yogurt", "volume", 192, 14), ("buttermilk", "volume", 192, 7), ("milk", "volume", 768, 7),
    ("chicken broth", "volume", 192, 7), ("vegetable broth", "volume", 192, 7), ("beef broth", "volume", 192, 7),
    ("butter", "weight", 16, 30), ("feta", "weight", 6, 10), ("cheddar", "weight", 8, 14),
    ("parmesan", "weight", 5, 30), ("mozzarella", "weight", 8, 10), ("ricotta", "weight", 15, 7),
    ("halloumi", "weight", 8, 14), ("cheese", "weight", 8, 14), ("spinach", "weight", 5, 5),
    ("arugula", "weight", 5, 5), ("mushroom", "weight", 8, 5),
]
# Shelf life in days for whole items and anything not in PACKS, by keyword.
SHELF_DAYS = [
    (("cilantro", "parsley", "mint", "basil", "dill", "chive"), 7),
    (("lettuce", "romaine", "spinach", "arugula", "kale", "bok choy", "sprout"), 5),
    (("avocado", "banana"), 3),
    (("mushroom", "asparagus", "zucchini", "berry", "corn"), 5),
    (("onion", "garlic", "potato", "squash", "carrot", "cabbage", "lemon", "lime", "ginger", "shallot"), 14),
]
DEFAULT_DAYS = 7
# Count units where "a bit left of one" makes sense. Cloves and slices don't (you'd buy a head).
COUNT_UNITS = {"", "bunch", "head", "can", "package", "bag", "jar", "box", "loaf"}
MIN_COUNT_LEFT = 0.25      # at least a quarter of one item
MIN_PACK_SHARE = 0.2       # at least a fifth of a pack
SKIP_CATEGORIES = {"meat & seafood", "spices", "pantry", "frozen", "bakery", "other"}


def _days(name: str, default: int = DEFAULT_DAYS) -> int:
    for words, days in SHELF_DAYS:
        if any(w in name for w in words):
            return days
    return default


def _pack(name: str):
    return next((p for p in PACKS if p[0] in name), None)


def estimate(items: list[dict], today: dt.date | None = None) -> list[dict]:
    """items: shopping-list rows with 'needs'. Returns [{name, amount, use_by}] worth keeping track of."""
    today = today or dt.date.today()
    out = []
    for it in items:
        name = it["name"].lower()
        pack = _pack(name)
        if it.get("category") in SKIP_CATEGORIES and not (pack and pack[1] in ("volume", "weight")):
            continue
        for need in it.get("needs", []):
            qty, family, unit = need["qty"], need["family"], need["unit"]
            if qty <= 0:
                continue
            left = text = None
            if family == "count":
                if unit not in COUNT_UNITS:
                    continue
                spare = math.ceil(qty - 0.05) - qty
                if spare >= MIN_COUNT_LEFT:
                    left, text = spare, f"{_nice_number(spare)} {unit}".strip()
            elif pack and pack[1] == family:
                size = pack[2]
                spare = math.ceil(qty / size - 0.02) * size - qty
                if spare / size >= MIN_PACK_SHARE:
                    left, text = spare, format_amount(spare, family, unit)
            if left:
                days = pack[3] if pack else _days(name)
                out.append({"name": it["name"], "amount": f"about {text}",
                            "use_by": (today + dt.timedelta(days=days)).isoformat()})
                break
    return out
