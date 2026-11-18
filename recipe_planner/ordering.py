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
