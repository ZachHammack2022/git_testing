"""Download a freely licensed photo from Wikimedia Commons for each starter recipe.

Usage: python scripts/fetch_recipe_photos.py [candidates_dir]
Without an argument it saves the best match per recipe straight into
recipe_planner/static/recipe_photos/ and writes CREDITS.md next to them.
With a directory it saves up to 4 candidates per recipe there, for review.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from recipe_planner.seed_recipes import SEED_RECIPES  # noqa: E402
from recipe_planner.db import photo_slug  # noqa: E402

# What to search Commons for, by recipe title (generic dish names find real photos).
QUERIES = {
    "Sheet-Pan Lemon Herb Chicken & Potatoes": "roast chicken thighs potatoes",
    "Beef & Broccoli Stir-Fry": "beef and broccoli",
    "Black Bean & Sweet Potato Tacos": "sweet potato black bean tacos",
    "Salmon with Miso Glaze & Bok Choy": "grilled salmon fillet",
    "Spaghetti Bolognese": "spaghetti bolognese",
    "Chickpea Spinach Curry": "chana masala",
    "Turkey Taco Lettuce Wraps": "lettuce wraps",
    "Shrimp Scampi Linguine": "shrimp scampi",
    "Chicken Tikka Masala": "chicken tikka masala",
    "Mushroom Risotto": "mushroom risotto",
    "Greek Chicken Bowls": "chicken souvlaki",
    "Pork Carnitas Bowls": "carnitas",
    "Thai Red Curry with Shrimp": "thai red curry",
    "Classic Cheeseburgers & Oven Fries": "cheeseburger and fries",
    "Lentil Soup with Crusty Bread": "lentil soup",
    "Honey Garlic Pork Chops": "pork chop dinner",
    "Veggie Fried Rice": "vegetable fried rice",
    "Baked Cod with Herb Crumb": "baked cod fillet",
    "Chicken Fajitas": "chicken fajitas",
    "Pesto Gnocchi with Cherry Tomatoes": "gnocchi pesto",
    "Korean Beef Bulgogi Bowls": "bulgogi",
    "Butternut Squash Mac & Cheese": "macaroni and cheese",
    "Moroccan Chicken & Couscous": "chicken couscous",
    "Teriyaki Tofu & Veggie Stir-Fry": "tofu vegetable stir fry",
    "Steak with Chimichurri & Roasted Potatoes": "steak chimichurri",
    "White Chicken Chili": "white bean chili",
    "Margherita Flatbread Pizzas & Caesar Salad": "pizza margherita",
    "Shakshuka with Feta & Crusty Bread": "shakshouka",
    "Baja Fish Tacos with Lime Slaw": "fish tacos",
    "Thai Basil Chicken (Pad Krapow)": "pad kra pao",
    "Lemon Chicken Orzo": "orzo pasta chicken",
    "Sheet-Pan Sausage, Peppers & Gnocchi": "sausage and peppers",
    "Black Bean Soup with Lime Crema": "black bean soup",
    "Salmon Rice Bowls with Sesame Cucumber": "salmon rice bowl",
    "Chicken Caesar Wraps": "chicken caesar wrap",
    "Japanese Chicken Curry": "japanese curry rice",
    "Lamb Kofta with Tzatziki & Pita": "kofta",
    "Vietnamese Lemongrass Pork Noodle Bowls": "bún thịt nướng",
    "Pasta Primavera": "pasta primavera",
    "Beef & Bean Chili": "chili con carne",
    "Halloumi & Chickpea Salad Bowls": "halloumi salad",
}
# Which search result to keep, by position in SEED_RECIPES (chosen by eye from the candidates).
PICKS = [0, 3, 0, 1, 2, 3, 0, 2, 1, 1, 2, 3, 3, 0, 3, 0, 1, 0, 0, 3, 0, 2, 1, 2, 0, 0, 0, 0, 0, 0, 0,
         0, 3, 0, 0, 0, 1, 1, 2, 0, 3]
API = "https://commons.wikimedia.org/w/api.php"
UA = "MealPlannerPhotoFetcher/1.0 (personal recipe app)"
OK_LICENSE = re.compile(r"^(CC0|CC BY|CC-BY|Public domain|PD)", re.I)


def get(url: str) -> bytes:
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=40) as r:
                return r.read()
        except Exception:
            time.sleep(8 * (attempt + 1))
    raise RuntimeError(f"failed: {url}")


def candidates(term: str, limit: int = 4) -> list[dict]:
    params = {
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
        "gsrsearch": f"{term} filetype:bitmap", "gsrlimit": 25,
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 900,
    }
    data = json.loads(get(API + "?" + urllib.parse.urlencode(params)))
    pages = sorted(data.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    out = []
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata", {})
        lic = meta.get("LicenseShortName", {}).get("value", "")
        if (ii.get("mime") not in ("image/jpeg",) or ii.get("width", 0) < 900
                or ii.get("width", 0) < ii.get("height", 0) * 0.9 or not OK_LICENSE.match(lic)):
            continue
        author = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip() or "Unknown"
        out.append({"title": p["title"], "url": ii["thumburl"], "page": ii["descriptionurl"],
                    "license": lic, "author": author[:120]})
        if len(out) >= limit:
            break
    return out


def main() -> None:
    review = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    dest = review or ROOT / "recipe_planner" / "static" / "recipe_photos"
    dest.mkdir(parents=True, exist_ok=True)
    credits = []
    for r in SEED_RECIPES:
        slug = photo_slug(r["title"])
        if (dest / (f"{slug}-0.jpg" if review else f"{slug}.jpg")).exists():
            continue  # already downloaded on an earlier run
        try:
            cands = candidates(QUERIES[r["title"]])
        except RuntimeError as e:
            print("FAIL ", r["title"], e)
            continue
        if not cands:
            print("NONE ", r["title"])
            continue
        picks = cands if review else [cands[min(PICKS[SEED_RECIPES.index(r)], len(cands) - 1)]]
        for n, c in enumerate(picks):
            name = f"{slug}-{n}.jpg" if review else f"{slug}.jpg"
            try:
                (dest / name).write_bytes(get(c["url"]))
            except RuntimeError as e:
                print("FAIL ", c["title"], e)
                continue
            if not review:
                credits.append(f"- **{r['title']}**: [{c['title'][5:]}]({c['page']}) by {c['author']}, {c['license']}")
        print("ok   ", r["title"], len(picks))
        time.sleep(2)
    if credits:
        (dest / "CREDITS.md").write_text("# Photo credits\n\nFrom Wikimedia Commons.\n\n" + "\n".join(credits) + "\n")


if __name__ == "__main__":
    main()
