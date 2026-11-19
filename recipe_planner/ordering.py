"""Whole Foods on Amazon has no public ordering API, so "ordering" here means building the
cart fast: one consolidated list, a direct link per item (your saved product if you have one,
otherwise a Whole Foods search), progress tracking, and a record of what was ordered."""
from __future__ import annotations

import re
from urllib.parse import quote_plus

WHOLE_FOODS_BRAND = "VUZHIFdob2xlIEZvb2Rz"  # Amazon's store id for Whole Foods
WHOLE_FOODS_STOREFRONT = f"https://www.amazon.com/alm/storefront?almBrandId={WHOLE_FOODS_BRAND}"
AMAZON_CART = "https://www.amazon.com/gp/cart/view.html"

# Words that hurt search results more than they help.
_SEARCH_STRIP = ("boneless ", "skinless ", "fresh ", "large ", "small ", "medium ")


def search_term(name: str) -> str:
    term = name.lower()
    for w in _SEARCH_STRIP:
        term = term.replace(w, "")
    return term.strip()


_ASIN = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})", re.I)


def whole_foods_product_url(url_or_asin: str) -> str:
    """Product link that opens the Whole Foods offer, not Amazon's default seller.

    Without the store parameter a product page can show a different seller (e.g. Amazon
    Now / Amazon Fresh), and "Add to cart" then goes to that store's cart."""
    s = (url_or_asin or "").strip()
    m = _ASIN.search(s)
    asin = m.group(1) if m else (s if re.fullmatch(r"[A-Z0-9]{10}", s, re.I) else None)
    if not asin:
        return s  # short links (a.co) and anything else: leave as given
    return f"https://www.amazon.com/dp/{asin.upper()}?almBrandId={WHOLE_FOODS_BRAND}&fpw=alm"


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
            "url": whole_foods_product_url(pick["url"]) if pick else whole_foods_search_url(it["name"]),
            "has_pick": bool(pick),
            "pick_label": pick.get("label", "") if pick else "",
            "added": False,
        })
    return rows


def refresh_links(items: list[dict], picks: dict[str, dict]) -> list[dict]:
    """Re-apply saved product picks to an existing order (e.g. after saving a new pick)."""
    for it in items:
        pick = picks.get(it["key"])
        it["url"] = whole_foods_product_url(pick["url"]) if pick else whole_foods_search_url(it["name"])
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
        "url": whole_foods_product_url(pick["url"]) if pick else whole_foods_search_url(parsed["name"]),
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


def _pick_asin(item: dict) -> str | None:
    if not item.get("has_pick"):
        return None
    m = _ASIN.search(item.get("url", ""))
    return m.group(1).upper() if m else None


def display_options(item: dict) -> list[dict]:
    """Previewed products for an item, with your saved product always first: it's what the
    cart filler will add, so it must be visible even if the preview couldn't read its page."""
    options = [dict(o) for o in item.get("options") or []]
    asin = _pick_asin(item)
    if not asin:
        return options
    mine = next((o for o in options if o["asin"] == asin), None)
    if mine is None:
        if not options and "options" not in item:
            return []  # not looked up yet
        label = re.sub(r"^auto-picked:\s*", "", item.get("pick_label") or "")
        mine = {"asin": asin, "title": label or "Your saved product", "image": "", "price": None,
                "unit_price": ""}
    else:
        options.remove(mine)
    return [dict(mine, pick=True)] + options


def chosen_option(item: dict) -> dict | None:
    """The previewed product this item will get: your saved product if you have one, else what
    the cart filler would take (the first non-sponsored result)."""
    options = display_options(item)
    return options[0] if options else None


def estimate_total(items: list[dict]) -> dict:
    """{total, priced, count} over the previewed choices (one of each item)."""
    prices = [o["price"] for o in (chosen_option(i) for i in items) if o and o.get("price")]
    return {"total": round(sum(prices), 2), "priced": len(prices), "count": len(items)}
