"""Weekly planning. Uses Claude when configured, otherwise a feedback-weighted picker.

Both paths learn from feedback: ratings, "make again" votes, notes, and recency.
"""
from __future__ import annotations

import datetime as dt
import random

from . import costing, db
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


def split_csv(s: str) -> list[str]:
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


def budget_adjust(cost: float | None, per_meal: float | None) -> float:
    """Score change for a recipe costing `cost` when each remaining dinner can spend `per_meal`.
    Dearer than the share is penalised (up to -3), cheaper gets a small bonus (up to +0.5)."""
    if not cost or not per_meal:
        return 0.0
    ratio = cost / max(per_meal, 1.0)
    return -min(3.0, 1.6 * (ratio - 1)) if ratio > 1 else min(0.5, 0.5 * (1 - ratio))


def score_recipe(recipe: dict, stats: dict, settings: dict, week_start: dt.date,
                 affinity: dict | None = None, cost: float | None = None,
                 per_meal_budget: float | None = None) -> float:
    """Higher = more likely to be picked. Exposed for tests and the UI."""
    s = stats.get(recipe["id"], {})
    score = 1.0
    if affinity:
        c = affinity.get("cuisine", {}).get((recipe.get("cuisine") or "").strip().lower())
        p = affinity.get("protein", {}).get((recipe.get("protein") or "").strip().lower())
        score += (c["score"] * 0.5 if c else 0) + (p["score"] * 0.4 if p else 0)
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

    favs = split_csv(settings.get("favorite_cuisines", ""))
    if favs and recipe.get("cuisine", "").lower() in favs:
        score += 0.5

    max_prep = int(settings.get("max_prep_minutes") or 0)
    if max_prep and recipe.get("prep_minutes", 0) > max_prep:
        score -= 1.5
    return score + budget_adjust(cost, per_meal_budget)


PERISHABLE = {"produce", "dairy & eggs", "bakery"}

QUICK_WORDS = {"quick", "fast", "easy", "simple", "busy", "light"}
_MEATLESS = ("vegetarian", "tofu", "vegan")
THEME_SYNONYMS = {"meatless": _MEATLESS, "veggie": _MEATLESS, "vegetarian": _MEATLESS,
                  "vegan": ("vegan",), "seafood": ("fish", "shrimp"), "taco": ("mexican", "taco"),
                  "tacos": ("mexican", "taco"), "curry": ("curry", "indian", "thai"),
                  "pasta": ("pasta", "spaghetti", "linguine", "gnocchi", "mac", "italian"),
                  "stir-fry": ("stir-fry", "chinese"), "stirfry": ("stir-fry", "chinese")}
_THEME_STOP = {"night", "nights", "day", "dinner", "something", "and", "or", "the", "a", "with",
               "meal", "meals", "monday", "tuesday", "wednesday", "thursday", "friday",
               "saturday", "sunday", "weeknight"}


def theme_bonus(recipe: dict, theme: str) -> float:
    """How well a recipe fits a day's theme, e.g. 'Taco Tuesday', 'quick', 'meatless'."""
    words = [w.strip(".,!") for w in (theme or "").lower().split()]
    words = [w for w in words if w and w not in _THEME_STOP]
    if not words:
        return 0.0
    hay = " ".join([recipe.get("title", ""), recipe.get("cuisine", ""), recipe.get("protein", ""),
                    " ".join(recipe.get("tags", []))]).lower()
    bonus = 0.0
    for w in words:
        if w in QUICK_WORDS:
            minutes = recipe.get("prep_minutes") or 60
            bonus += 1.5 if minutes <= 30 else (-1.0 if minutes > 45 else 0)
            continue
        targets = {w, normalize_name(w), *THEME_SYNONYMS.get(w, ())}
        if any(t and t in hay for t in targets):
            bonus += 2.5
    return bonus


def _perishables(recipe: dict) -> set[str]:
    return {normalize_name(i.get("name", "")) for i in recipe.get("ingredients", [])
            if (i.get("category") or "") in PERISHABLE}


def heuristic_pick(
    recipes: list[dict],
    stats: dict,
    settings: dict,
    days: list[str],
    week_start: dt.date,
    rng: random.Random | None = None,
    exclude_ids: set[int] | None = None,
    already: list[dict] | None = None,
    affinity: dict | None = None,
    use_up: list[str] | None = None,
    costs: dict[int, float] | None = None,
) -> list[dict]:
    """Greedy pick with variety penalties and a small bonus for sharing fresh ingredients
    with the rest of the week (less waste). `already` = recipes fixed in the week.
    Returns [{day, recipe_id}]."""
    rng = rng or random.Random()
    exclude_ids = exclude_ids or set()
    dislikes = split_csv(settings.get("dislikes", ""))
    pool = [
        r for r in recipes
        if not r.get("archived") and r["id"] not in exclude_ids and not contains_disliked(r, dislikes)
    ]
    base = {r["id"]: score_recipe(r, stats, settings, week_start, affinity) + rng.uniform(0, 0.8)
            for r in pool}
    fresh = {r["id"]: _perishables(r) for r in pool}
    wanted = [normalize_name(u) for u in (use_up or []) if u.strip()]
    if wanted:
        for r in pool:
            names = [normalize_name(i.get("name", "")) for i in r.get("ingredients", [])]
            hits = sum(1 for w in wanted if any(w in n for n in names))
            base[r["id"]] += 1.2 * hits
    chosen: list[dict] = list(already or [])
    week_fresh: set[str] = set().union(*(_perishables(c) for c in chosen)) if chosen else set()
    themes = settings.get("day_themes") or {}
    # Themed days choose first, most constrained first, so "pizza Friday" gets the one pizza
    # recipe even if "meatless Monday" would also have liked it.
    def matches(d):
        t = themes.get(d, "").strip()
        return sum(1 for r in pool if theme_bonus(r, t) > 0) if t else 10**6
    order = sorted(days, key=matches)
    by_day: dict[str, dict] = {}
    budget = float(settings.get("weekly_budget") or 0) if costs else 0.0
    n_meals = len(days) + len(chosen)
    for day in order:
        if not pool:
            break
        # What each dinner still to pick can spend if the week is to stay on budget.
        per_meal = None
        if budget:
            spent = sum(costs.get(c["id"], 0.0) for c in chosen)
            per_meal = (budget - spent) / max(n_meals - len(chosen), 1)

        def adjusted(r):
            sc = base[r["id"]] + theme_bonus(r, themes.get(day, "")) + budget_adjust(costs.get(r["id"]) if costs else None, per_meal)
            same_cuisine = sum(1 for c in chosen if c.get("cuisine") == r.get("cuisine"))
            same_protein = sum(1 for c in chosen if c.get("protein") == r.get("protein"))
            shared = len(fresh[r["id"]] & week_fresh)
            return sc - 0.8 * same_cuisine - 0.6 * same_protein + min(shared, 4) * 0.15

        best = max(pool, key=adjusted)
        chosen.append(best)
        by_day[day] = best
        week_fresh |= fresh[best["id"]]
        pool = [r for r in pool if r["id"] != best["id"]]
    return [{"day": d, "recipe_id": by_day[d]["id"]} for d in days if d in by_day]


def ordered_days(days: list[str]) -> list[str]:
    return [d for d in db.DAYS if d in days]


def _pick_meals(conn, settings: dict, days: list[str], week_start: dt.date, use_ai: bool,
                extra_request: str = "", rng: random.Random | None = None,
                keep: list[dict] | None = None,
                use_up: list[str] | None = None) -> tuple[list[dict], str, str]:
    """Choose recipes for `days`, around already-fixed recipes in `keep`.
    Returns (picks [{day, recipe_id}], planner_name, summary)."""
    keep = keep or []
    picks: list[dict] = []
    planner_name, summary = "heuristic", ""
    if use_ai and days:
        from . import ai

        if ai.available():
            try:
                request_text = extra_request
                if use_up:
                    request_text += f"\nUse up these ingredients I already have: {', '.join(use_up)}."
                if float(settings.get("weekly_budget") or 0):
                    request_text += f"\nKeep the week's groceries to about ${float(settings['weekly_budget']):.0f}; prefer affordable recipes."
                picks, summary = ai.plan_week(conn, settings, days, week_start, request_text,
                                              locked=[k["title"] for k in keep])
                planner_name = "claude"
                kept_ids = {k["id"] for k in keep}
                dislikes = split_csv(settings.get("dislikes", ""))
                picks = [p for p in picks if p["recipe_id"] not in kept_ids
                         and not contains_disliked(db.get_recipe(conn, p["recipe_id"]), dislikes)]
            except ai.AIError as e:
                summary = f"Claude planning failed ({e}); used the built-in planner instead."
                picks = []

    if len(picks) < len(days):
        taken_days = {p["day"] for p in picks}
        remaining = [d for d in days if d not in taken_days]
        already = keep + [db.get_recipe(conn, p["recipe_id"]) for p in picks]
        library = db.list_recipes(conn)
        costs = None
        if float(settings.get("weekly_budget") or 0):
            costs = costing.recipe_costs(library, int(settings.get("household_size") or 2), db.price_book(conn),
                                         db.pantry(conn) + [i["name"] for i in db.on_hand(conn)])
        fill = heuristic_pick(
            library, db.recipe_stats(conn, week_start.isoformat()), settings,
            remaining, week_start, rng=rng, already=already, affinity=db.taste_affinity(conn),
            use_up=use_up, costs=costs,
            exclude_ids={r["id"] for r in already},
        )
        picks.extend(fill)
    return picks, planner_name, summary


def plan_week(conn, week_start: dt.date | None = None, use_ai: bool | None = None,
              extra_request: str = "", rng: random.Random | None = None,
              use_up: list[str] | None = None) -> int:
    """Create a plan for the week and return its id."""
    settings = db.get_settings(conn)
    week_start = week_start or upcoming_monday()
    days = ordered_days(settings.get("cook_days") or db.DAYS[:5])
    n = int(settings.get("dinners_per_week") or len(days))
    days = days[:n] if n <= len(days) else days
    servings = int(settings.get("household_size") or 2)
    if use_ai is None:
        use_ai = bool(settings.get("use_ai"))

    picks, planner_name, summary = _pick_meals(conn, settings, days, week_start, use_ai,
                                               extra_request, rng, use_up=use_up)
    meals = [{"day": p["day"], "recipe_id": p["recipe_id"], "servings": servings} for p in picks]
    return db.create_plan(conn, week_start.isoformat(), meals, planner_name, summary)


def regenerate(conn, plan_id: int, use_ai: bool | None = None, extra_request: str = "",
               rng: random.Random | None = None, use_up: list[str] | None = None) -> str:
    """Re-plan every unlocked meal of a plan, keeping locked ones. Returns a message."""
    plan = db.get_plan(conn, plan_id)
    settings = db.get_settings(conn)
    if use_ai is None:
        use_ai = bool(settings.get("use_ai"))
    locked = [m for m in plan["meals"] if m["locked"]]
    unlocked = [m for m in plan["meals"] if not m["locked"]]
    if not unlocked:
        return "Every meal is locked — nothing to change."
    week_start = dt.date.fromisoformat(plan["week_start"])
    days = [m["day"] for m in unlocked]
    current = {m["recipe_id"] for m in unlocked}
    picks, planner_name, summary = _pick_meals(
        conn, settings, days, week_start, use_ai, extra_request, rng,
        keep=[m["recipe"] for m in locked], use_up=use_up,
    )
    # Prefer something different from what was there; fine if the library is small.
    by_day = {p["day"]: p["recipe_id"] for p in picks}
    for m in unlocked:
        if m["day"] in by_day:
            db.update_meal(conn, m["id"], recipe_id=by_day[m["day"]])
    changed = sum(1 for m in unlocked if by_day.get(m["day"]) not in current)
    return summary or f"Re-planned {changed} meal(s); kept {len(locked)} locked."


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
        db.list_recipes(conn), db.recipe_stats(conn, plan["week_start"]), settings, [meal["day"]],
        week_start, rng=rng, exclude_ids=in_plan, affinity=db.taste_affinity(conn),
        already=[m["recipe"] for m in plan["meals"] if m["id"] != meal_id],
    )
    if not pick:
        return "No other recipes available to swap in."
    db.update_meal(conn, meal_id, recipe_id=pick[0]["recipe_id"])
    title = db.get_recipe(conn, pick[0]["recipe_id"])["title"]
    return f"{fallback_note}Swapped in {title}."
