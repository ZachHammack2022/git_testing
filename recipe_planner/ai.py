"""Claude-powered planning. Optional: everything works without it.

Set ANTHROPIC_API_KEY (or log in with `ant auth login`) to enable.
"""
from __future__ import annotations

import datetime as dt
import json
import os

from . import db
from .grocery import CATEGORIES

MODEL = os.environ.get("RECIPE_PLANNER_MODEL", "claude-opus-5-5")


class AIError(Exception):
    pass


def available() -> bool:
    if os.environ.get("RECIPE_PLANNER_DISABLE_AI"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # `ant auth login` stores a profile the SDK reads automatically.
    return os.path.isdir(os.path.expanduser("~/.config/anthropic"))


_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()
    return _client


RECIPE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "description", "cuisine", "protein", "servings", "prep_minutes",
                 "ingredients", "steps", "tags"],
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "cuisine": {"type": "string"},
        "protein": {"type": "string", "description": "main protein, e.g. chicken, beef, fish, shrimp, tofu, vegetarian"},
        "servings": {"type": "integer"},
        "prep_minutes": {"type": "integer"},
        "ingredients": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "quantity", "unit", "category"],
                "properties": {
                    "name": {"type": "string", "description": "grocery item name as you'd search for it, no prep notes"},
                    "quantity": {"type": "number"},
                    "unit": {"type": "string", "description": "tsp, tbsp, cup, oz, lb, can, clove, bunch, or '' for count"},
                    "category": {"type": "string", "enum": CATEGORIES},
                },
            },
        },
        "steps": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}


def _plan_schema(days: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "reused", "new"],
        "properties": {
            "summary": {"type": "string", "description": "1-3 sentences on how this week reflects the feedback"},
            "reused": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["day", "recipe_id"],
                    "properties": {
                        "day": {"type": "string", "enum": days},
                        "recipe_id": {"type": "integer"},
                    },
                },
            },
            "new": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["day", "recipe"],
                    "properties": {
                        "day": {"type": "string", "enum": days},
                        "recipe": RECIPE_SCHEMA,
                    },
                },
            },
        },
    }


SYSTEM = """You plan weekly dinners for one household and adapt to their feedback over time.
Prefer recipes they rated highly, avoid anything they disliked or said not to repeat, and act on
their notes (e.g. "too spicy", "loved it but took too long"). Keep the week varied in cuisine and
protein, respect dietary rules and dislikes strictly, and keep prep time within their limit.
Groceries come from Whole Foods via Amazon, so use ingredients that store carries and reuse
ingredients across the week where it reduces waste. New recipes must be complete and cookable:
real quantities for the stated servings, grocery-searchable ingredient names, clear steps."""


def taste_context(conn, settings: dict) -> str:
    """Everything the model should know about preferences and history, as compact text."""
    stats = db.recipe_stats(conn)
    lines = ["## Household preferences"]
    lines.append(f"- Servings per meal: {settings.get('household_size')}")
    lines.append(f"- Max prep time: {settings.get('max_prep_minutes')} minutes")
    for key, label in [("dietary", "Dietary rules"), ("dislikes", "Dislikes / never use"),
                       ("favorite_cuisines", "Favorite cuisines"), ("notes", "Standing notes")]:
        if settings.get(key):
            lines.append(f"- {label}: {settings[key]}")

    fb = db.recent_feedback(conn, limit=40)
    if fb:
        lines.append("\n## Recent feedback (newest first)")
        for f in fb:
            note = f" — \"{f['notes']}\"" if f["notes"] else ""
            lines.append(f"- {f['title']}: {f['rating']}/5, make again: {f['make_again']}{note}")

    aff = db.taste_affinity(conn)
    for field in ("cuisine", "protein"):
        ranked = sorted(aff[field].items(), key=lambda kv: -kv[1]["score"])
        if ranked:
            lines.append(f"\n## Average rating by {field}")
            lines.extend(f"- {k}: {v['avg']:.1f}/5 over {v['n']} meal(s)" for k, v in ranked)

    lines.append("\n## Recipe library (id | title | cuisine | protein | prep | avg rating | last planned)")
    for r in db.list_recipes(conn):
        s = stats.get(r["id"], {})
        avg = f"{s['avg_rating']:.1f}" if s.get("avg_rating") else "-"
        lines.append(
            f"{r['id']} | {r['title']} | {r['cuisine']} | {r['protein']} | {r['prep_minutes']}m"
            f" | {avg} | {s.get('last_week') or 'never'}"
        )
    archived = [r["title"] for r in db.list_recipes(conn, include_archived=True) if r["archived"]]
    if archived:
        lines.append("\n## Retired recipes (do not suggest these or close variants)")
        lines.extend(f"- {t}" for t in archived)
    return "\n".join(lines)


def _call(prompt: str, schema: dict, max_tokens: int = 16000) -> dict:
    import anthropic

    client = _get_client()
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError as e:
        raise AIError("API key rejected") from e
    except anthropic.RateLimitError as e:
        raise AIError("rate limited, try again shortly") from e
    except anthropic.APIStatusError as e:
        raise AIError(f"API error {e.status_code}") from e
    except anthropic.APIConnectionError as e:
        raise AIError("could not reach the API") from e

    if resp.stop_reason == "refusal":
        raise AIError("request was declined")
    if resp.stop_reason == "max_tokens":
        raise AIError("response was cut off")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AIError("could not parse response") from e


def _store_new_recipe(conn, recipe: dict) -> int | None:
    """Save a Claude recipe, reusing an existing one with the same title.
    Returns None if it matches a recipe the user retired."""
    existing = db.find_recipe_by_title(conn, recipe["title"])
    if existing:
        return None if existing["archived"] else existing["id"]
    return db.add_recipe(conn, recipe, source="ai")


def plan_week(conn, settings: dict, days: list[str], week_start: dt.date,
              extra_request: str = "", locked: list[str] | None = None) -> tuple[list[dict], str]:
    """Returns ([{day, recipe_id}], summary). Unknown ids and duplicate days are dropped."""
    ratio = settings.get("new_recipe_ratio")
    ratio = 0.4 if ratio is None else float(ratio)
    n_new = max(0, round(len(days) * ratio))
    prompt = (
        f"{taste_context(conn, settings)}\n\n"
        f"## Task\nPlan dinners for the week of {week_start.isoformat()} on exactly these days: "
        f"{', '.join(days)}. Each day gets one dinner.\n"
        f"Aim for about {n_new} brand-new recipe(s) in `new` and reuse library recipes by id in `reused` "
        f"for the rest (favor high ratings; skip anything planned in the last two weeks unless it's a "
        f"standout favorite). New recipes should serve {settings.get('household_size')}."
    )
    if locked:
        prompt += (f"\nAlready fixed for other days this week (don't repeat them; keep the week "
                   f"varied around them): {'; '.join(locked)}")
    if extra_request.strip():
        prompt += f"\nThis week's special request: {extra_request.strip()}"

    data = _call(prompt, _plan_schema(days))
    valid_ids = {r["id"] for r in db.list_recipes(conn)}
    picks: list[dict] = []
    used_days: set[str] = set()
    used_ids: set[int] = set()

    def take(day: str, rid: int | None) -> None:
        if rid and day in days and day not in used_days and rid not in used_ids:
            picks.append({"day": day, "recipe_id": rid})
            used_days.add(day)
            used_ids.add(rid)

    for item in data.get("reused", []):
        if item["recipe_id"] in valid_ids:
            take(item["day"], item["recipe_id"])
    for item in data.get("new", []):
        if item["day"] in days and item["day"] not in used_days and item["recipe"].get("ingredients"):
            take(item["day"], _store_new_recipe(conn, item["recipe"]))
    return picks, data.get("summary", "")


def suggest_replacement(conn, settings: dict, plan: dict, meal: dict, request: str) -> tuple[int, str]:
    """Pick or invent one replacement dinner. Returns (recipe_id, short reason)."""
    current = db.get_recipe(conn, meal["recipe_id"])
    others = [m["recipe"]["title"] for m in plan["meals"] if m["id"] != meal["id"]]
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["reason", "reuse_recipe_id", "recipe"],
        "properties": {
            "reason": {"type": "string", "description": "one short sentence for the user"},
            "reuse_recipe_id": {"type": "integer", "description": "library id to reuse, or 0 to use `recipe`"},
            "recipe": RECIPE_SCHEMA,
        },
    }
    prompt = (
        f"{taste_context(conn, settings)}\n\n## Task\nReplace {meal['day']}'s dinner "
        f"(currently \"{current['title']}\"). The rest of the week: {', '.join(others) or 'nothing else'}. "
        "Pick something that fits the week's variety. Either reuse a library recipe "
        "(set reuse_recipe_id, and give a minimal placeholder `recipe`) or invent a new one "
        "(reuse_recipe_id = 0, full `recipe`)."
    )
    if request.strip():
        prompt += f"\nThe user asked for: {request.strip()}"
    data = _call(prompt, schema)
    valid = {r["id"] for r in db.list_recipes(conn)}
    rid = data.get("reuse_recipe_id") or 0
    if rid and rid in valid and rid != current["id"]:
        return rid, data.get("reason", "")
    recipe = data.get("recipe") or {}
    if not recipe.get("ingredients"):
        raise AIError("no usable recipe returned")
    rid = _store_new_recipe(conn, recipe)
    if rid is None:
        raise AIError("suggested a recipe you retired")
    return rid, data.get("reason", "")


def recipe_from_text(text: str, servings: int) -> dict:
    """Turn a pasted recipe (or a dish idea) into a structured recipe."""
    prompt = (
        "Convert the following into a structured recipe. If it is only a dish idea, write a "
        f"complete recipe for {servings} servings.\n\n---\n{text.strip()}\n---"
    )
    return _call(prompt, RECIPE_SCHEMA)


def revise_recipe(recipe: dict, notes: list[str]) -> dict:
    """Rewrite a recipe so it incorporates the cook's notes from past attempts."""
    slim = {k: recipe[k] for k in ("title", "description", "cuisine", "protein", "servings",
                                   "prep_minutes", "ingredients", "steps", "tags")}
    prompt = (
        "Here is a recipe I've cooked, and my notes from cooking it. Revise the recipe so it "
        "reflects the notes (adjust quantities, ingredients, steps or timing). Keep everything "
        "the notes don't touch the same, and keep the same title unless the dish really changed.\n\n"
        f"Recipe:\n{json.dumps(slim, indent=1)}\n\nMy notes (newest first):\n"
        + "\n".join(f"- {n}" for n in notes)
    )
    return _call(prompt, RECIPE_SCHEMA)
