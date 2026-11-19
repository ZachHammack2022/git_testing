"""USDA FoodData Central lookups: nutrients per 100 g for an ingredient.

Set USDA_API_KEY (free at https://fdc.nal.usda.gov/api-key-signup.html) for normal use. Without one the
shared DEMO_KEY works but only allows about 10 lookups an hour.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
# nutrient ids in FoodData Central
ENERGY = (1008, 2047, 2048)   # kcal; Foundation foods report Atwater energy under 2047/2048
FIELDS = {"protein": (1003,), "fat": (1004,), "carbs": (1005,), "fiber": (1079,), "sodium": (1093,)}

STOP = {"fresh", "large", "small", "medium", "chopped", "diced", "minced", "sliced", "shredded", "grated",
        "ripe", "packed", "extra", "virgin", "optional", "baby", "finely", "thinly", "roughly", "crushed",
        "lean", "plain", "unsalted", "salted", "low", "sodium", "reduced", "fat", "free", "organic"}
DRY_STAPLES = ("rice", "pasta", "spaghetti", "linguine", "penne", "orzo", "couscous", "noodle", "quinoa",
               "oats", "lentil", "gnocchi", "macaroni")
SYNONYMS = [
    (r"jasmine|basmati|long.?grain|white rice|^rice$", "rice white long-grain regular raw"),
    (r"spaghetti|linguine|penne|orzo|macaroni|fettuccine|pasta", "pasta dry enriched"),
    (r"soy sauce", "soy sauce made from soy and wheat shoyu"),
    (r"olive oil", "oil olive salad or cooking"),
    (r"vegetable oil|canola oil", "oil vegetable canola"),
    (r"brown sugar", "sugars brown"),
    (r"^sugar$|granulated sugar", "sugars granulated"),
    (r"flour", "wheat flour white all-purpose enriched"),
    (r"chicken thighs?", "chicken thigh meat only raw"),
    (r"chicken breasts?", "chicken breast meat only raw"),
    (r"ground beef", "beef ground 85% lean meat raw"),
    (r"ground turkey", "turkey ground raw"),
]


class RateLimited(Exception):
    """USDA says slow down. Results so far are kept; try again later."""


def api_key() -> str:
    return os.environ.get("USDA_API_KEY", "").strip() or "DEMO_KEY"


def has_real_key() -> bool:
    return api_key() != "DEMO_KEY"


def search_query(name: str, unit: str = "") -> str:
    """Turn an ingredient name from a recipe into a USDA search phrase."""
    words = [w for w in re.findall(r"[a-z]+", name.lower()) if w not in STOP]
    q = " ".join(words)
    for pattern, replacement in SYNONYMS:
        if re.search(pattern, q):
            return replacement
    if unit == "can":
        q += " canned"
    elif any(w in q for w in DRY_STAPLES):
        q += " dry"
    return q


def _nutrient(food: dict, ids: tuple[int, ...]) -> float | None:
    by_id = {n.get("nutrientId"): n.get("value") for n in food.get("foodNutrients", [])}
    for i in ids:
        if by_id.get(i) is not None:
            return float(by_id[i])
    return None


def per100(food: dict) -> dict | None:
    """{calories, protein, carbs, fat, fiber, sodium} per 100 g of a search result, or None if it has no energy."""
    kcal = _nutrient(food, ENERGY)
    if kcal is None:
        return None
    out = {"calories": kcal}
    for key, ids in FIELDS.items():
        out[key] = _nutrient(food, ids) or 0.0
    return out


def pick_best(foods: list[dict], query: str) -> dict | None:
    """Choose the most plausible result: matching words, raw (or dry) rather than cooked, plain over branded."""
    words = set(query.lower().split())
    wants_cooked = "cooked" in words or "canned" in words
    best, best_score = None, -99.0
    for f in foods:
        if per100(f) is None:
            continue
        desc = f.get("description", "").lower()
        parts = [p.strip() for p in desc.split(",")]
        score = len(words & set(re.findall(r"[a-z]+", desc))) / max(len(words), 1)
        if words & set(re.findall(r"[a-z]+", parts[0])):
            score += 0.4                                   # the main noun is what we asked for
        if not wants_cooked:
            if re.search(r"\b(raw|uncooked|dry)\b", desc):
                score += 0.5
            if re.search(r"\bcooked|boiled|fried|roasted|baked|braised|steamed\b", desc) and "uncooked" not in desc:
                score -= 0.6
        if re.search(r"babyfood|restaurant|fast food|school|formulated|prepared|frozen entree", desc):
            score -= 1.0
        score -= 0.03 * len(parts)                         # plainer descriptions win ties
        if score > best_score:
            best, best_score = f, score
    return best


def search(query: str, timeout: int = 20) -> list[dict]:
    params = urllib.parse.urlencode({"api_key": api_key(), "query": query, "pageSize": 10,
                                     "dataType": "Foundation,SR Legacy"})
    try:
        with urllib.request.urlopen(f"{SEARCH_URL}?{params}", timeout=timeout) as r:
            return json.load(r).get("foods", [])
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited() from e
        raise


def lookup(name: str, unit: str = "") -> dict | None:
    """Best USDA match for an ingredient: {fdc_id, description, per100} or None if nothing fits."""
    query = search_query(name, unit)
    if not query:
        return None
    food = pick_best(search(query), query)
    if not food:
        return None
    return {"fdc_id": food["fdcId"], "description": food["description"], "per100": per100(food)}
