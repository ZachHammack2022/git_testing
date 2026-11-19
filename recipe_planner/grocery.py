"""Turn a week's plan into one consolidated, aisle-grouped shopping list."""
from __future__ import annotations

import math
import re
from fractions import Fraction

CATEGORIES = [
    "produce",
    "meat & seafood",
    "dairy & eggs",
    "bakery",
    "pantry",
    "spices",
    "frozen",
    "other",
]

# unit -> (family, factor to base unit). volume base = tsp, weight base = oz
UNIT_TABLE = {
    "tsp": ("volume", 1.0), "teaspoon": ("volume", 1.0), "teaspoons": ("volume", 1.0),
    "tbsp": ("volume", 3.0), "tablespoon": ("volume", 3.0), "tablespoons": ("volume", 3.0),
    "cup": ("volume", 48.0), "cups": ("volume", 48.0), "c": ("volume", 48.0),
    "fl oz": ("volume", 6.0), "pint": ("volume", 96.0), "quart": ("volume", 192.0),
    "ml": ("volume", 0.202884), "l": ("volume", 202.884), "liter": ("volume", 202.884),
    "oz": ("weight", 1.0), "ounce": ("weight", 1.0), "ounces": ("weight", 1.0),
    "lb": ("weight", 16.0), "lbs": ("weight", 16.0), "pound": ("weight", 16.0),
    "pounds": ("weight", 16.0), "g": ("weight", 0.035274), "gram": ("weight", 0.035274),
    "grams": ("weight", 0.035274), "kg": ("weight", 35.274),
}

UNIT_ALIASES = {
    "": "", "each": "", "whole": "", "count": "", "medium": "", "large": "", "small": "",
    "cloves": "clove", "cans": "can", "bunches": "bunch", "heads": "head",
    "packages": "package", "pkg": "package", "slices": "slice", "sprigs": "sprig",
    "stalks": "stalk", "fillets": "fillet", "pieces": "piece", "jars": "jar",
    "bags": "bag", "boxes": "box", "loaves": "loaf", "pinches": "pinch",
}

CATEGORY_KEYWORDS = {
    "meat & seafood": ["chicken", "beef", "pork", "turkey", "salmon", "shrimp", "cod", "tuna",
                       "sausage", "bacon", "lamb", "steak", "fish", "tilapia", "halibut", "chorizo"],
    "dairy & eggs": ["milk", "cheese", "butter", "yogurt", "cream", "egg", "parmesan",
                     "feta", "mozzarella", "cheddar", "ricotta", "sour cream"],
    "bakery": ["bread", "tortilla", "bun", "pita", "naan", "baguette", "roll"],
    "frozen": ["frozen"],
    "spices": ["cumin", "paprika", "oregano", "chili powder", "cinnamon", "thyme", "garam masala",
               "curry powder", "red pepper flakes", "turmeric", "coriander", "bay leaf",
               "italian seasoning", "cayenne", "pepper flakes", "black pepper", "salt"],
    "produce": ["onion", "garlic", "tomato", "pepper", "lettuce", "spinach", "kale", "carrot",
                "celery", "potato", "lemon", "lime", "cilantro", "parsley", "basil", "ginger",
                "zucchini", "broccoli", "cucumber", "avocado", "mushroom", "scallion",
                "green onion", "cabbage", "apple", "squash", "asparagus", "bean sprout",
                "jalapeno", "mint", "shallot", "green bean", "corn", "sweet potato", "bok choy",
                "arugula", "cauliflower", "eggplant", "dill", "romaine"],
}

_IRREGULAR = {"leaves": "leaf", "loaves": "loaf", "potatoes": "potato", "tomatoes": "tomato",
              "chilies": "chili", "chiles": "chile", "berries": "berry", "cherries": "cherry"}
_NO_SINGULAR = ("ss", "us", "is", "ous", "molasses", "hummus", "couscous", "asparagus",
                "greens", "oats", "grits", "noodles", "brussels sprouts", "chickpeas", "lentils",
                "beans", "peas", "flakes", "breadcrumbs", "panko crumbs")


# Descriptors that don't change what you buy ("large shrimp" == "shrimp").
_DESCRIPTORS = {"fresh", "large", "small", "medium", "ripe", "boneless", "skinless", "raw",
                "organic", "jumbo", "extra-large", "peeled", "deveined", "chopped",
                "minced", "shredded", "grated", "trimmed"}
# Not stripped on purpose: "diced"/"crushed" (canned tomatoes are a different product than
# fresh) and "cooked" (a cup of cooked rice is not a cup of dry rice).


def normalize_name(name: str) -> str:
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    n = re.sub(r"\(.*?\)", "", n).strip()       # drop parentheticals
    n = n.split(",")[0].strip()                  # "onion, diced" -> "onion"
    words = [w for w in n.split(" ") if w not in _DESCRIPTORS]
    n = " ".join(words) or n
    if n in _IRREGULAR:
        return _IRREGULAR[n]
    words = n.split(" ")
    last = words[-1] if words else ""
    if last in _IRREGULAR:
        words[-1] = _IRREGULAR[last]
    elif (len(last) > 3 and last.endswith("s") and not n.endswith(_NO_SINGULAR)
          and not last.endswith(_NO_SINGULAR)):
        words[-1] = last[:-1]
    return " ".join(words)


def normalize_unit(unit: str) -> str:
    u = (unit or "").strip().lower().rstrip(".")
    return UNIT_ALIASES.get(u, u)


def guess_category(name: str) -> str:
    n = normalize_name(name)
    # longest keyword wins so "sweet potato" beats "potato" and "black pepper" beats "pepper"
    best, best_len = "pantry", 0
    for cat, words in CATEGORY_KEYWORDS.items():
        for w in words:
            if w in n and len(w) > best_len:
                best, best_len = cat, len(w)
    return best


def parse_quantity(q) -> float:
    """Accepts 1, 1.5, '1 1/2', '3/4', '' -> float."""
    if q is None or q == "":
        return 0.0
    if isinstance(q, (int, float)):
        return float(q)
    s = str(q).strip()
    total = 0.0
    for part in s.split():
        try:
            total += float(Fraction(part))
        except (ValueError, ZeroDivisionError):
            return total or 0.0
    return total


def _nice_number(x: float) -> str:
    if x <= 0:
        return ""
    whole = math.floor(x)
    frac = x - whole
    # snap to common kitchen fractions
    for val, label in [(0, ""), (0.25, "1/4"), (1 / 3, "1/3"), (0.5, "1/2"), (2 / 3, "2/3"),
                       (0.75, "3/4"), (1, "")]:
        if abs(frac - val) < 0.06:
            if val == 1:
                whole, label = whole + 1, ""
            if whole and label:
                return f"{whole} {label}"
            return label or str(int(whole))
    return f"{x:.2f}".rstrip("0").rstrip(".")


def format_amount(qty_base: float, family: str, unit: str) -> str:
    if qty_base <= 0:
        return ""
    if family == "volume":
        if qty_base >= 12:
            return f"{_nice_number(qty_base / 48)} cup"
        if qty_base >= 3:
            return f"{_nice_number(qty_base / 3)} tbsp"
        return f"{_nice_number(qty_base)} tsp"
    if family == "weight":
        if qty_base >= 16:
            return f"{_nice_number(qty_base / 16)} lb"
        return f"{_nice_number(qty_base)} oz"
    # Whole items (avocados, cans, bunches) are bought whole: round up.
    qty_base = math.ceil(qty_base - 0.05) or 1
    amount = _nice_number(qty_base)
    if not unit:
        return amount
    plural = unit if qty_base <= 1 or unit.endswith("s") else unit + ("es" if unit.endswith(("ch", "sh", "x")) else "s")
    return f"{amount} {plural}"


def build_shopping_list(meals: list[dict], pantry: list[str] | None = None) -> list[dict]:
    """meals: [{recipe: {...ingredients, servings, title}, servings}] -> aggregated items.

    Each item: {key, name, amounts: [str], category, recipes: [titles], in_pantry: bool}
    """
    pantry_set = {normalize_name(p) for p in (pantry or [])}
    agg: dict[str, dict] = {}
    for meal in meals:
        recipe = meal.get("recipe") or {}
        base_servings = max(int(recipe.get("servings") or 1), 1)
        scale = (meal.get("total_servings") or meal.get("servings") or base_servings) / base_servings
        for ing in recipe.get("ingredients", []):
            raw_name = ing.get("name", "").strip()
            if not raw_name:
                continue
            key = normalize_name(raw_name)
            unit = normalize_unit(ing.get("unit", ""))
            qty = parse_quantity(ing.get("quantity")) * scale
            family, factor = UNIT_TABLE.get(unit, (f"count:{unit}", 1.0))
            item = agg.setdefault(key, {
                "key": key,
                "name": raw_name.split(",")[0].strip().lower(),
                "category": (ing.get("category") or "").lower() or guess_category(raw_name),
                "families": {},
                "recipes": [],
                "in_pantry": key in pantry_set,
            })
            if item["category"] not in CATEGORIES:
                item["category"] = guess_category(raw_name)
            fam = item["families"].setdefault(family, {"qty": 0.0, "unit": unit, "unspecified": False})
            if qty > 0:
                fam["qty"] += qty * factor
            else:
                fam["unspecified"] = True
            title = recipe.get("title", "")
            if title and title not in item["recipes"]:
                item["recipes"].append(title)

    items = []
    for item in agg.values():
        amounts = []
        for family, fam in item["families"].items():
            fam_name = family.split(":")[0]
            text = format_amount(fam["qty"], fam_name, fam["unit"])
            if text:
                amounts.append(text)
            elif fam["unspecified"]:
                amounts.append("to taste" if item["category"] == "spices" else "some")
        items.append({
            "key": item["key"],
            "name": item["name"],
            "amount": " + ".join(dict.fromkeys(amounts)),
            "category": item["category"],
            "recipes": item["recipes"],
            "in_pantry": item["in_pantry"],
            # numeric totals per unit family (base units), for leftover tracking
            "needs": [{"family": f.split(":")[0], "qty": fam["qty"], "unit": fam["unit"]}
                      for f, fam in item["families"].items() if fam["qty"] > 0],
        })
    items.sort(key=lambda i: (CATEGORIES.index(i["category"]), i["name"]))
    return items


def group_by_category(items: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it["category"], []).append(it)
    return [(c, groups[c]) for c in CATEGORIES if c in groups]


def as_text(items: list[dict]) -> str:
    lines = []
    for cat, group in group_by_category([i for i in items if not i.get("in_pantry")]):
        lines.append(cat.upper())
        for i in group:
            lines.append(f"- {i['name']}" + (f" ({i['amount']})" if i["amount"] else ""))
        lines.append("")
    return "\n".join(lines).strip()


_KNOWN_UNITS = set(UNIT_TABLE) | {u for u in UNIT_ALIASES if u} | set(UNIT_ALIASES.values()) | {
    "clove", "can", "bunch", "head", "package", "slice", "sprig", "stalk", "fillet", "piece",
    "jar", "bag", "box", "loaf", "pinch", "dash", "handful", "dozen", "gallon", "gallons",
    "bottle", "bottles", "carton", "cartons", "container", "containers", "pack", "packs"}


def parse_ingredient_line(line: str) -> dict | None:
    """'1 1/2 cups jasmine rice' -> {quantity: 1.5, unit: 'cup', name: 'jasmine rice', category}."""
    line = re.sub(r"^[-*•\s]+", "", line or "").strip()
    if not line:
        return None
    tokens = line.split()
    qty = 0.0
    i = 0
    while i < len(tokens):
        tok = tokens[i].replace("½", "1/2").replace("¼", "1/4").replace("¾", "3/4")
        try:
            qty += float(Fraction(tok))
            i += 1
        except (ValueError, ZeroDivisionError):
            break
    unit = ""
    if i < len(tokens):
        cand = tokens[i].lower().rstrip(".")
        two = " ".join(tokens[i:i + 2]).lower()
        if two == "fl oz":
            unit, i = "fl oz", i + 2
        elif cand in _KNOWN_UNITS:
            unit, i = normalize_unit(cand), i + 1
            if unit in UNIT_TABLE:
                # canonical short form: "cups" -> "cup", "pounds" -> "lb"
                unit = {"teaspoon": "tsp", "teaspoons": "tsp", "tablespoon": "tbsp",
                        "tablespoons": "tbsp", "cups": "cup", "c": "cup", "ounce": "oz",
                        "ounces": "oz", "lbs": "lb", "pound": "lb", "pounds": "lb",
                        "gram": "g", "grams": "g"}.get(unit, unit)
    name = " ".join(tokens[i:]).strip()
    if name.lower().startswith("of "):
        name = name[3:]
    if not name:
        return None
    return {"quantity": round(qty, 3), "unit": unit, "name": name, "category": guess_category(name)}


def ingredient_to_line(ing: dict) -> str:
    q = ing.get("quantity") or ""
    if isinstance(q, float) and q.is_integer():
        q = int(q)
    unit = ing.get("unit", "")
    # pluralize count-style units and cups; abbreviations (tbsp, oz, lb...) stay as-is
    if q and float(q) > 1 and unit and (unit not in UNIT_TABLE or unit == "cup"):
        unit = unit + ("es" if unit.endswith(("ch", "sh", "x")) else "s")
    parts = [str(_nice_number(float(q))) if q else "", unit, ing.get("name", "")]
    return " ".join(p for p in parts if p)
