"""Whole Foods on Amazon has no public ordering API, so "ordering" here means building the
cart fast: one consolidated list, a direct link per item (your saved product if you have one,
otherwise a Whole Foods search), progress tracking, and a record of what was ordered."""
from __future__ import annotations

from urllib.parse import quote_plus

WHOLE_FOODS_STOREFRONT = "https://www.amazon.com/alm/storefront?almBrandId=VUZHIFdob2xlIEZvb2Rz"
AMAZON_CART = "https://www.amazon.com/gp/cart/view.html"

# Words that hurt search results more than they help.
_SEARCH_STRIP = ("boneless ", "skinless ", "fresh ", "large ", "small ", "medium ")


def search_term(name: str) -> str:
    term = name.lower()
    for w in _SEARCH_STRIP:
        term = term.replace(w, "")
    return term.strip()


def whole_foods_search_url(name: str) -> str:
    return f"https://www.amazon.com/s?k={quote_plus(search_term(name))}&i=wholefoods"


def order_items(shopping_items: list[dict], picks: dict[str, dict]) -> list[dict]:
    """Shopping list -> order rows. Pantry items are left out."""
    rows = []
    for it in shopping_items:
        if it.get("in_pantry"):
            continue
        pick = picks.get(it["key"])
        rows.append({
            "key": it["key"],
            "name": it["name"],
            "amount": it["amount"],
            "category": it["category"],
            "recipes": it["recipes"],
            "url": pick["url"] if pick else whole_foods_search_url(it["name"]),
            "has_pick": bool(pick),
            "pick_label": pick.get("label", "") if pick else "",
            "added": False,
        })
    return rows


def refresh_links(items: list[dict], picks: dict[str, dict]) -> list[dict]:
    """Re-apply saved product picks to an existing order (e.g. after saving a new pick)."""
    for it in items:
        pick = picks.get(it["key"])
        it["url"] = pick["url"] if pick else whole_foods_search_url(it["name"])
        it["has_pick"] = bool(pick)
        it["pick_label"] = pick.get("label", "") if pick else ""
    return items


def extra_item(line: str, picks: dict[str, dict], source: str = "extra") -> dict | None:
    """A free-text grocery line ("1 gallon whole milk") -> an order row."""
    from .grocery import CATEGORIES, guess_category, normalize_name, parse_ingredient_line

    parsed = parse_ingredient_line(line)
    if not parsed:
        return None
    amount = line.strip()[: len(line.strip()) - len(parsed["name"])].strip()
    key = normalize_name(parsed["name"])
    pick = picks.get(key)
    cat = guess_category(parsed["name"])
    return {
        "key": key,
        "name": parsed["name"].lower(),
        "amount": amount,
        "category": cat if cat in CATEGORIES else "other",
        "recipes": [source],
        "url": pick["url"] if pick else whole_foods_search_url(parsed["name"]),
        "has_pick": bool(pick),
        "pick_label": pick.get("label", "") if pick else "",
        "added": False,
    }


def merge_extras(rows: list[dict], lines: list[str], picks: dict[str, dict], source: str) -> list[dict]:
    """Append extra items; if one is already on the list, just note it there."""
    by_key = {r["key"]: r for r in rows}
    for line in lines:
        item = extra_item(line, picks, source)
        if not item:
            continue
        if item["key"] in by_key:
            existing = by_key[item["key"]]
            if source not in existing["recipes"]:
                existing["recipes"].append(source)
            if item["amount"]:
                existing["amount"] = " + ".join(x for x in [existing["amount"], item["amount"]] if x)
        else:
            rows.append(item)
            by_key[item["key"]] = item
    return rows
