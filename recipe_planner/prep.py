"""A batch-prep checklist for the week: chop once, cook grains once, thaw on time.

The built-in version works from ingredient names and recipe steps; Claude can write a smarter
one (ai.prep_plan). Prep is split into two sessions because chopped vegetables keep 3-4 days:
a weekend session for Mon-Wed dinners and a Wednesday-evening top-up for Thu-Sun.
"""
from __future__ import annotations

import hashlib
import re

from .db import DAYS
from .grocery import build_shopping_list, normalize_name

# Vegetables worth chopping ahead (keep 3-4 days chopped, in a sealed container).
CHOP = ["onion", "red onion", "shallot", "garlic", "ginger", "bell pepper", "pepper", "carrot",
        "celery", "zucchini", "broccoli", "cauliflower", "cabbage", "scallion", "green onion",
        "mushroom", "butternut squash", "squash", "leek", "eggplant", "green bean", "jalapeno",
        "asparagus", "kale", "bok choy", "brussels sprouts", "fennel", "radish", "snap pea"]
# Better done on the day: they brown, wilt or go watery.
DAY_OF = ["avocado", "cilantro", "basil", "parsley", "mint", "dill", "tomato", "lettuce",
          "romaine", "arugula", "apple", "potato", "sweet potato", "cucumber", "banana", "pear"]
# Cook once, use twice (keeps 4-5 days in the fridge).
GRAINS = ["rice", "brown rice", "jasmine rice", "basmati rice", "quinoa", "farro", "barley",
          "lentil", "black bean", "chickpea", "wild rice"]
SAUCE_STEP = re.compile(r"^(whisk|mix|stir|combine|blend|shake)\b.*\b(sauce|dressing|marinade|"
                        r"vinaigrette|glaze)\b", re.I)
MARINATE = re.compile(r"\bmarinat", re.I)
FROZEN_PROTEIN_FROM = 3  # Thu onward: freeze fresh meat/fish on delivery, thaw the night before


def _has(n: str, w: str) -> bool:
    return re.search(rf"\b{re.escape(w)}(e?s)?\b", n) is not None


def _match(name: str, words: list[str]) -> str | None:
    """Longest word from `words` in the ingredient name, unless it's a do-it-on-the-day item."""
    n = normalize_name(name)
    if any(_has(n, w) for w in DAY_OF):
        return None
    hits = [w for w in words if _has(n, w)]
    return max(hits, key=len) if hits else None


def task_id(text: str) -> str:
    return hashlib.sha1(text.lower().encode()).hexdigest()[:10]


def _days_text(days: list[str]) -> str:
    return ", ".join(days)


def _session(meals: list[dict]) -> list[dict]:
    """Tasks for one prep session covering these meals."""
    amounts = {i["key"]: i["amount"] for i in build_shopping_list(meals)}
    chop: dict[str, dict] = {}
    grains: dict[str, dict] = {}
    tasks: list[dict] = []
    for m in meals:
        r = m["recipe"]
        for ing in r.get("ingredients", []):
            name = ing.get("name", "")
            key = normalize_name(name)
            unit = (ing.get("unit") or "").lower().rstrip("s")
            if "canned" in key or "frozen" in key or "cooked" in key or unit in ("can", "jar", "pouch"):
                continue  # already cooked, nothing to prep
            if _match(name, GRAINS) and ing.get("category") != "produce":
                bucket = grains
            elif _match(name, CHOP) and ing.get("category") in (None, "", "produce"):
                bucket = chop
            else:
                continue
            e = bucket.setdefault(key, {"name": key, "days": [], "amount": amounts.get(key, "")})
            if m["day"] not in e["days"]:
                e["days"].append(m["day"])
        for step in r.get("steps", []):
            if SAUCE_STEP.search(step.strip()):
                tasks.append({"kind": "sauce", "text": f"Make the sauce for {r['title']} ({m['day']})",
                              "detail": step.strip() + " Keeps 4–5 days in a jar.", "days": [m["day"]]})
                break

    chop_tasks = []
    for e in sorted(chop.values(), key=lambda e: (-len(e["days"]), e["name"])):
        verb = "Mince" if e["name"] in ("garlic", "ginger") else "Chop"
        amt = f" ({e['amount']})" if e["amount"] else ""
        split = " — split into a container per meal" if len(e["days"]) > 1 else ""
        chop_tasks.append({"kind": "chop", "text": f"{verb} {e['name']}{amt}",
                           "detail": f"for {_days_text(e['days'])}{split}", "days": e["days"]})
    grain_tasks = [{"kind": "grain", "text": f"Cook {e['name']}" + (f" ({e['amount']})" if e["amount"] else ""),
                    "detail": f"for {_days_text(e['days'])} — cool, then refrigerate (keeps 4–5 days)",
                    "days": e["days"]} for e in sorted(grains.values(), key=lambda e: e["name"])]
    return chop_tasks + grain_tasks + tasks


def _night_before(meals: list[dict]) -> list[dict]:
    """Reminders for the evening before: thaw protein, start marinades."""
    out = []
    for m in meals:
        if m["day"] not in DAYS:
            continue
        idx = DAYS.index(m["day"])
        before = DAYS[idx - 1] if idx > 0 else "Sun (weekend)"
        r = m["recipe"]
        proteins = [normalize_name(i["name"]) for i in r.get("ingredients", [])
                    if i.get("category") == "meat & seafood"]
        if proteins and idx >= FROZEN_PROTEIN_FROM:
            out.append({"day": before, "text": f"Move the {', '.join(proteins)} from freezer to fridge "
                                                f"for {m['day']}'s {r['title']}"})
        if any(MARINATE.search(s) for s in r.get("steps", [])):
            out.append({"day": before, "text": f"Start the marinade for {r['title']} ({m['day']})"})
    return out


def builtin_plan(plan: dict) -> dict:
    meals = [m for m in plan["meals"] if m.get("recipe") and not m.get("cooked")]
    early = [m for m in meals if m["day"] in DAYS[:3]]
    late = [m for m in meals if m["day"] not in DAYS[:3]]
    sessions = []
    if early:
        sessions.append({"title": "Weekend prep", "subtitle": "for Mon–Wed dinners", "tasks": _session(early)})
    if late:
        freeze = sorted({normalize_name(i["name"]) for m in late if DAYS.index(m["day"]) >= FROZEN_PROTEIN_FROM
                         for i in m["recipe"].get("ingredients", []) if i.get("category") == "meat & seafood"})
        if freeze and sessions:
            sessions[0]["tasks"].append({"kind": "freeze", "text": f"Freeze {', '.join(freeze)}",
                                         "detail": "for Thu–Sun — fresh meat and fish only keep 1–2 days",
                                         "days": []})
        sessions.append({"title": "Wednesday top-up" if early else "Prep", "subtitle": "for Thu–Sun dinners",
                         "tasks": _session(late)})
    return finish({"source": "builtin", "sessions": sessions, "night_before": _night_before(meals)})


def finish(prep: dict) -> dict:
    """Give every task a stable id (for ticking off) and drop empty sessions."""
    for s in prep.get("sessions", []):
        for t in s.get("tasks", []):
            t["id"] = task_id(s.get("title", "") + "|" + t["text"])
    for t in prep.get("night_before", []):
        t["id"] = task_id("night|" + t["day"] + "|" + t["text"])
    prep["sessions"] = [s for s in prep.get("sessions", []) if s.get("tasks")]
    return prep


def plan_signature(plan: dict) -> str:
    """Changes when the plan's dinners change (so a saved Claude prep plan can be flagged stale)."""
    return ",".join(f"{m['day']}:{m['recipe_id']}" for m in plan["meals"])
