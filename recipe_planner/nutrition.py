"""Estimate calories, protein, carbs, fat, fibre and sodium per serving of a recipe.

Amounts are converted to grams with typical densities and item weights, then multiplied by USDA
nutrients per 100 g. Expect numbers within roughly 10-20%: good for goals and comparisons, not for medicine.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading

from . import db, usda
from .grocery import UNIT_TABLE, normalize_name, normalize_unit, parse_quantity

KEYS = ("calories", "protein", "carbs", "fat", "fiber", "sodium")
IGNORED = ("salt", "pepper", "water", "ice")          # negligible, and always "to taste"
ML_PER_TSP = 4.929

# grams per millilitre, by keyword in the ingredient name (first match wins)
DENSITY = [
    ("honey", 1.42), ("syrup", 1.3), ("soy sauce", 1.15), ("flour", 0.53), ("brown sugar", 0.93), ("sugar", 0.85),
    ("panko", 0.25), ("breadcrumb", 0.45), ("oat", 0.35), ("couscous", 0.72), ("rice", 0.8), ("quinoa", 0.72),
    ("lentil", 0.8), ("pasta", 0.4), ("orzo", 0.8), ("parmesan", 0.4), ("cheese", 0.45), ("oil", 0.92), ("butter", 0.96),
    ("spinach", 0.13), ("lettuce", 0.1), ("arugula", 0.1), ("kale", 0.15), ("cilantro", 0.15), ("parsley", 0.15),
    ("basil", 0.15), ("mint", 0.15), ("cumin", 0.45), ("paprika", 0.45), ("oregano", 0.2), ("chili powder", 0.5),
    ("cinnamon", 0.5), ("coriander", 0.4), ("garam", 0.45), ("turmeric", 0.6), ("flakes", 0.3),
]
CATEGORY_DENSITY = {"produce": 0.6, "spices": 0.45, "pantry": 0.9, "dairy & eggs": 1.0, "meat & seafood": 0.95}

# grams per whole item, by keyword in the ingredient name
ITEM_GRAMS = [
    ("egg", 50), ("garlic", 40), ("onion", 150), ("shallot", 40), ("lemon", 60), ("lime", 45), ("tomato", 120),
    ("potato", 170), ("bell pepper", 150), ("jalapeno", 15), ("avocado", 150), ("carrot", 60), ("zucchini", 200),
    ("cucumber", 300), ("tortilla", 45), ("bun", 60), ("pita", 60), ("naan", 90), ("baguette", 250), ("sausage", 85),
    ("chicken", 150), ("steak", 225), ("fillet", 170), ("salmon", 170), ("cod", 170), ("pork chop", 200),
    ("squash", 900), ("cauliflower", 600), ("broccoli", 300), ("lettuce", 500), ("cabbage", 900), ("apple", 180),
]
# grams per count-style unit
UNIT_GRAMS = {"clove": 3, "can": 400, "bunch": 40, "head": 500, "slice": 28, "sprig": 1, "stalk": 40,
              "package": 340, "jar": 340, "bag": 300, "box": 300, "loaf": 450, "pinch": 0.4, "piece": 100,
              "fillet": 170, "stick": 113}


def _density(name: str, category: str) -> float:
    for word, d in DENSITY:
        if word in name:
            return d
    return CATEGORY_DENSITY.get(category, 1.0)


def grams(ing: dict) -> float:
    """Best guess at the weight in grams of one ingredient line (0 if it can't be told)."""
    name = (ing.get("name") or "").lower()
    qty = parse_quantity(ing.get("quantity"))
    if qty <= 0:
        return 0.0
    unit = normalize_unit(ing.get("unit", ""))
    family, factor = UNIT_TABLE.get(unit, ("count", 1.0))
    if family == "weight":
        return qty * factor * 28.3495                         # factor converts to ounces
    if family == "volume":
        return qty * factor * ML_PER_TSP * _density(name, (ing.get("category") or "").lower())
    if unit in UNIT_GRAMS:
        if unit == "head" and "garlic" in name:
            return qty * 40
        return qty * UNIT_GRAMS[unit]
    for word, g in ITEM_GRAMS:                                # "2 lemons", "3 eggs", "1 onion"
        if word in name:
            return qty * g
    cat = (ing.get("category") or "").lower()
    return qty * {"produce": 100, "meat & seafood": 150, "dairy & eggs": 100}.get(cat, 50)


def ingredients_key(recipe: dict) -> str:
    """Changes whenever the ingredient list or serving count does, so stale numbers aren't shown."""
    rows = [(normalize_name(i.get("name", "")), str(i.get("quantity")), i.get("unit", ""))
            for i in recipe.get("ingredients", [])]
    return hashlib.sha1(json.dumps([rows, recipe.get("servings")]).encode()).hexdigest()[:12]


def current(recipe: dict) -> dict | None:
    """The saved nutrition for a recipe, if it still matches the recipe's ingredients."""
    n = recipe.get("nutrition")
    return n if n and n.get("key") == ingredients_key(recipe) else None


def food_for(conn, name: str, unit: str, lookup=None) -> dict | None:
    """USDA match for an ingredient, remembered so each name is only ever looked up once."""
    key = f"{normalize_name(name)}|{'can' if unit == 'can' else ''}"
    row = conn.execute("SELECT data FROM food_cache WHERE name=?", (key,)).fetchone()
    if row:
        return json.loads(row["data"]) or None
    found = (lookup or usda.lookup)(name, unit)  # may raise usda.RateLimited: nothing is cached then
    conn.execute("INSERT OR REPLACE INTO food_cache(name, data) VALUES (?, ?)", (key, json.dumps(found)))
    conn.commit()
    return found


def compute(conn, recipe: dict, lookup=None) -> dict:
    """Per-serving nutrition for a recipe. Raises usda.RateLimited if USDA stops us partway."""
    total = dict.fromkeys(KEYS, 0.0)
    matched, missing, used = 0, [], []
    for ing in recipe.get("ingredients", []):
        name = (ing.get("name") or "").strip()
        if not name or any(re.search(rf"\b{w}\b", name.lower()) for w in IGNORED):
            continue
        g = grams(ing)
        if g <= 0:
            continue
        unit = normalize_unit(ing.get("unit", ""))
        food = food_for(conn, name, unit, lookup)
        if not food:
            missing.append(name)
            continue
        matched += 1
        used.append({"name": name, "grams": round(g), "usda": food["description"]})
        for k in KEYS:
            total[k] += food["per100"][k] * g / 100
    servings = max(int(recipe.get("servings") or 1), 1)
    per = {k: round(v / servings, 1 if k in ("protein", "carbs", "fat", "fiber") else 0) for k, v in total.items()}
    return {"per_serving": per, "matched": matched, "total": matched + len(missing), "missing": missing,
            "used": used, "key": ingredients_key(recipe), "source": "USDA FoodData Central"}


def calculate(conn, recipe_id: int, lookup=None) -> dict:
    recipe = db.get_recipe(conn, recipe_id)
    result = compute(conn, recipe, lookup)
    db.set_recipe_nutrition(conn, recipe_id, result)
    return result


# ---------- score term for the planner ----------

def goal_adjust(nut: dict | None, settings: dict) -> float:
    """Score change for a recipe given the protein / calorie goals per dinner. 0 when no data or no goals."""
    if not nut:
        return 0.0
    per = nut["per_serving"]
    score = 0.0
    want_protein = float(settings.get("min_protein") or 0)
    if want_protein:
        score += 0.25 if per["protein"] >= want_protein else -min(2.0, 3 * (want_protein - per["protein"]) / want_protein)
    cap = float(settings.get("max_calories") or 0)
    if cap and per["calories"] > cap:
        score -= min(2.0, 3 * (per["calories"] - cap) / cap)
    return score


# ---------- fill every recipe in the background ----------

_fill = {"state": "idle", "done": 0, "total": 0, "message": ""}
_lock = threading.Lock()


def fill_status() -> dict:
    return dict(_fill)


def start_fill(db_file: str, lookup=None, wait: bool = False) -> bool:
    """Calculate nutrition for every recipe that lacks it (or is out of date). Returns False if already running."""
    with _lock:
        if _fill["state"] == "running":
            return False
        _fill.update(state="running", done=0, total=0, message="Starting…")

    def work():
        conn = db.connect(db_file)
        try:
            todo = [r for r in db.list_recipes(conn, include_archived=True) if not current(r)]
            _fill.update(total=len(todo))
            for r in todo:
                _fill["message"] = f"Looking up {r['title']}…"
                try:
                    calculate(conn, r["id"], lookup)
                except usda.RateLimited:
                    _fill.update(state="limited", message="USDA asked us to slow down. Progress is saved; run it again later.")
                    return
                _fill["done"] += 1
            _fill.update(state="done", message=f"Calculated {len(todo)} recipe{'s' if len(todo) != 1 else ''}.")
        except Exception as e:                           # network down, etc.
            _fill.update(state="error", message=f"Stopped: {e}")
        finally:
            conn.close()

    t = threading.Thread(target=work, daemon=True)
    t.start()
    if wait:
        t.join()
    return True
