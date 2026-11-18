"""Weekly planning. Uses Claude when configured, otherwise a feedback-weighted picker.

Both paths learn from feedback: ratings, "make again" votes, notes, and recency.
"""
from __future__ import annotations

import datetime as dt
import random

from . import db
from .grocery import normalize_name


def upcoming_monday(today: dt.date | None = None) -> dt.date:
    today = today or dt.date.today()
    # On Mon-Wed plan the current week; Thu-Sun plan next week.
    if today.weekday() <= 2:
        return today - dt.timedelta(days=today.weekday())
    return today + dt.timedelta(days=7 - today.weekday())


def next_unplanned_week(latest_week: str | None, today: dt.date | None = None) -> dt.date:
    """Default week for a new plan: the upcoming Monday, or the week after the latest plan."""
    week = upcoming_monday(today)
    if latest_week:
        after_latest = dt.date.fromisoformat(latest_week) + dt.timedelta(days=7)
        week = max(week, after_latest)
    return week


def _split_csv(s: str) -> list[str]:
    return [x.strip().lower() for x in (s or "").replace(";", ",").split(",") if x.strip()]


def contains_disliked(recipe: dict, dislikes: list[str]) -> bool:
    if not dislikes:
        return False
    names = [normalize_name(i.get("name", "")) for i in recipe.get("ingredients", [])]
    title = recipe.get("title", "").lower()
    for d in dislikes:
        nd = normalize_name(d)
        if nd and (any(nd in n for n in names) or nd in title):
            return True
    return False


def score_recipe(recipe: dict, stats: dict, settings: dict, week_start: dt.date) -> float:
    """Higher = more likely to be picked. Exposed for tests and the UI."""
    s = stats.get(recipe["id"], {})
    score = 1.0
    if s.get("ratings"):
        score += (s["avg_rating"] - 3.0) * 0.9
        score += min(s.get("yes_votes", 0), 3) * 0.2
        score -= s.get("no_votes", 0) * 2.0
    else:
        score += 0.3  # mild curiosity bonus for untried recipes

    last = s.get("last_week")
    if last:
        try:
            weeks_ago = (week_start - dt.date.fromisoformat(last)).days / 7
        except ValueError:
            weeks_ago = 99
        if weeks_ago < 1:
            score -= 3.0
        elif weeks_ago < 2:
            score -= 1.5
        elif weeks_ago < 3:
            score -= 0.5

    favs = _split_csv(settings.get("favorite_cuisines", ""))
    if favs and recipe.get("cuisine", "").lower() in favs:
        score += 0.5

    max_prep = int(settings.get("max_prep_minutes") or 0)
    if max_prep and recipe.get("prep_minutes", 0) > max_prep:
        score -= 1.5
    return score


def heuristic_pick(
    recipes: list[dict],
    stats: dict,
    settings: dict,
    days: list[str],
    week_start: dt.date,
    rng: random.Random | None = None,
    exclude_ids: set[int] | None = None,
) -> list[dict]:
    """Greedy pick with variety penalties. Returns [{day, recipe_id}]."""
    rng = rng or random.Random()
    exclude_ids = exclude_ids or set()
    dislikes = _split_csv(settings.get("dislikes", ""))
    pool = [
        r for r in recipes
        if not r.get("archived") and r["id"] not in exclude_ids and not contains_disliked(r, dislikes)
    ]
    base = {r["id"]: score_recipe(r, stats, settings, week_start) + rng.uniform(0, 0.8) for r in pool}
    chosen: list[dict] = []
    for day in days:
        if not pool:
            break
        def adjusted(r):
            sc = base[r["id"]]
            same_cuisine = sum(1 for c in chosen if c.get("cuisine") == r.get("cuisine"))
            same_protein = sum(1 for c in chosen if c.get("protein") == r.get("protein"))
            return sc - 0.8 * same_cuisine - 0.6 * same_protein
        best = max(pool, key=adjusted)
        chosen.append(best)
        pool = [r for r in pool if r["id"] != best["id"]]
    return [{"day": d, "recipe_id": r["id"]} for d, r in zip(days, chosen)]


def ordered_days(days: list[str]) -> list[str]:
    return [d for d in db.DAYS if d in days]


def plan_week(conn, week_start: dt.date | None = None, use_ai: bool | None = None,
              extra_request: str = "", rng: random.Random | None = None) -> int:
    """Create a plan for the week and return its id."""
    settings = db.get_settings(conn)
    week_start = week_start or upcoming_monday()
    days = ordered_days(settings.get("cook_days") or db.DAYS[:5])
    n = int(settings.get("dinners_per_week") or len(days))
    days = days[:n] if n <= len(days) else days
    servings = int(settings.get("household_size") or 2)

    if use_ai is None:
        use_ai = bool(settings.get("use_ai"))

    picks: list[dict] = []
    planner_name = "heuristic"
    summary = ""
    if use_ai:
        from . import ai

        if ai.available():
            try:
                picks, summary = ai.plan_week(conn, settings, days, week_start, extra_request)
                planner_name = "claude"
            except ai.AIError as e:
                summary = f"Claude planning failed ({e}); used the built-in planner instead."
                picks = []

    if len(picks) < len(days):
        taken_days = {p["day"] for p in picks}
        remaining = [d for d in days if d not in taken_days]
        fill = heuristic_pick(
            db.list_recipes(conn), db.recipe_stats(conn, week_start.isoformat()), settings, remaining, week_start,
            rng=rng, exclude_ids={p["recipe_id"] for p in picks},
        )
        picks.extend(fill)

    meals = [{"day": p["day"], "recipe_id": p["recipe_id"], "servings": servings} for p in picks]
    return db.create_plan(conn, week_start.isoformat(), meals, planner_name, summary)


def swap_meal(conn, meal_id: int, request: str = "", use_ai: bool | None = None,
              rng: random.Random | None = None) -> str:
    """Replace one meal. Returns a short message describing what happened."""
    meal = db.get_meal(conn, meal_id)
    if not meal:
        return "Meal not found."
    plan = db.get_plan(conn, meal["plan_id"])
    settings = db.get_settings(conn)
    in_plan = {m["recipe_id"] for m in plan["meals"]}
    if use_ai is None:
        use_ai = bool(settings.get("use_ai"))

    fallback_note = ""
    if use_ai:
        from . import ai

        if ai.available():
            try:
                recipe_id, why = ai.suggest_replacement(conn, settings, plan, meal, request)
                db.update_meal(conn, meal_id, recipe_id=recipe_id)
                return why or "Swapped."
            except ai.AIError as e:
                fallback_note = f"Claude swap failed ({e}); picked from your library instead. "

    week_start = dt.date.fromisoformat(plan["week_start"])
    pick = heuristic_pick(
        db.list_recipes(conn), db.recipe_stats(conn, plan["week_start"]), settings, [meal["day"]], week_start,
        rng=rng, exclude_ids=in_plan,
    )
    if not pick:
        return "No other recipes available to swap in."
    db.update_meal(conn, meal_id, recipe_id=pick[0]["recipe_id"])
    title = db.get_recipe(conn, pick[0]["recipe_id"])["title"]
    return f"{fallback_note}Swapped in {title}."
