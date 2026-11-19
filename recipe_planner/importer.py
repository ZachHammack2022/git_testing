"""Import a recipe from a web page.

Most recipe sites embed schema.org Recipe data as JSON-LD, which we read directly.
If a page doesn't, the caller can fall back to Claude with the page text.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import urllib.request

from .grocery import parse_ingredient_line

MAX_BYTES = 3_000_000

PROTEIN_WORDS = [
    ("shrimp", "shrimp"), ("prawn", "shrimp"), ("salmon", "fish"), ("cod", "fish"), ("tuna", "fish"),
    ("halibut", "fish"), ("tilapia", "fish"), ("fish", "fish"), ("chicken", "chicken"),
    ("turkey", "turkey"), ("beef", "beef"), ("steak", "beef"), ("pork", "pork"), ("bacon", "pork"),
    ("sausage", "pork"), ("lamb", "lamb"), ("tofu", "tofu"), ("tempeh", "tofu"),
]


class RecipeImportError(Exception):
    pass


def fetch(url: str, timeout: int = 15) -> str:
    if not url.startswith(("http://", "https://")):
        raise RecipeImportError("Enter a full http(s) link.")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES)
            charset = resp.headers.get_content_charset() or "utf-8"
    except Exception as e:  # network errors, HTTP errors, bad URLs
        raise RecipeImportError(f"Couldn't load that page ({e.__class__.__name__}).") from e
    return raw.decode(charset, errors="replace")


def _clean(s) -> str:
    if s is None:
        return ""
    s = htmllib.unescape(str(s))
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _iso_minutes(d) -> int:
    """'PT1H30M' -> 90."""
    if not d or not isinstance(d, str):
        return 0
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", d.strip().upper())
    if not m:
        return 0
    days, hours, mins = (int(x) if x else 0 for x in m.groups())
    return days * 1440 + hours * 60 + mins


def _first_int(v, default: int = 4) -> int:
    if isinstance(v, list):
        v = v[0] if v else None
    m = re.search(r"\d+", str(v or ""))
    return int(m.group()) if m else default


def _types(node: dict) -> list[str]:
    t = node.get("@type", [])
    return [t] if isinstance(t, str) else list(t)


def _find_recipe(data):
    if isinstance(data, list):
        for item in data:
            found = _find_recipe(item)
            if found:
                return found
    elif isinstance(data, dict):
        if "Recipe" in _types(data):
            return data
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in data:
                found = _find_recipe(data[key])
                if found:
                    return found
    return None


def _steps(instr) -> list[str]:
    out: list[str] = []
    if isinstance(instr, str):
        out.extend(_clean(p) for p in re.split(r"\n+", instr))
    elif isinstance(instr, list):
        for item in instr:
            out.extend(_steps(item))
    elif isinstance(instr, dict):
        if "itemListElement" in instr:
            out.extend(_steps(instr["itemListElement"]))
        else:
            out.append(_clean(instr.get("text") or instr.get("name")))
    return [s for s in out if s]


def guess_protein(ingredient_names: list[str]) -> str:
    text = " ".join(ingredient_names).lower()
    for word, protein in PROTEIN_WORDS:
        if re.search(rf"\b{word}", text):
            return protein
    return "vegetarian"


def _image_url(img) -> str:
    """schema.org `image` is a URL, a list, or an ImageObject; return one http(s) URL or ''."""
    if isinstance(img, list):
        img = img[0] if img else ""
    if isinstance(img, dict):
        img = img.get("url") or ""
    img = str(img).strip()
    return img if img.startswith(("http://", "https://")) else ""


def parse_jsonld(page: str) -> dict | None:
    blocks = re.findall(
        r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", page, re.S | re.I
    )
    for block in blocks:
        try:
            data = json.loads(block.strip(), strict=False)
        except json.JSONDecodeError:
            continue
        node = _find_recipe(data)
        if not node:
            continue
        ingredients = [x for x in (parse_ingredient_line(_clean(i)) for i in node.get("recipeIngredient", []) or [])
                       if x]
        if not ingredients:
            continue
        cuisine = node.get("recipeCuisine") or ""
        if isinstance(cuisine, list):
            cuisine = cuisine[0] if cuisine else ""
        keywords = node.get("keywords") or ""
        if isinstance(keywords, str):
            keywords = keywords.split(",")
        minutes = _iso_minutes(node.get("totalTime")) or (
            _iso_minutes(node.get("prepTime")) + _iso_minutes(node.get("cookTime")))
        return {
            "image": _image_url(node.get("image")),
            "title": _clean(node.get("name")) or "Imported recipe",
            "description": _clean(node.get("description"))[:300],
            "cuisine": _clean(cuisine),
            "protein": guess_protein([i["name"] for i in ingredients]),
            "servings": _first_int(node.get("recipeYield")),
            "prep_minutes": minutes or 30,
            "ingredients": ingredients,
            "steps": _steps(node.get("recipeInstructions")),
            "tags": [_clean(k).lower() for k in keywords if _clean(k)][:6],
        }
    return None


def page_text(page: str, limit: int = 30000) -> str:
    page = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    return _clean(page)[:limit]
