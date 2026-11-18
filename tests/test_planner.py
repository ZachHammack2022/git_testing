import datetime as dt
import random

from recipe_planner import db, planner


def test_upcoming_monday():
    assert planner.upcoming_monday(dt.date(2026, 11, 18)) == dt.date(2026, 11, 16)  # Wed -> this week
    assert planner.upcoming_monday(dt.date(2026, 11, 19)) == dt.date(2026, 11, 23)  # Thu -> next week


def test_plan_week_fills_cook_days(conn):
    db.save_settings(conn, {"cook_days": ["Mon", "Wed", "Fri"], "dinners_per_week": 3, "household_size": 3})
    pid = planner.plan_week(conn, dt.date(2026, 11, 23), use_ai=False, rng=random.Random(1))
    plan = db.get_plan(conn, pid)
    assert [m["day"] for m in plan["meals"]] == ["Mon", "Wed", "Fri"]
    assert all(m["servings"] == 3 for m in plan["meals"])
    assert len({m["recipe_id"] for m in plan["meals"]}) == 3


def test_dislikes_excluded(conn):
    db.save_settings(conn, {"dislikes": "shrimp, chicken"})
    for seed in range(5):
        pid = planner.plan_week(conn, dt.date(2026, 11, 23), use_ai=False, rng=random.Random(seed))
        for m in db.get_plan(conn, pid)["meals"]:
            names = " ".join(i["name"] for i in m["recipe"]["ingredients"]).lower()
            assert "shrimp" not in names and "chicken" not in names


def test_feedback_steers_plans(conn):
    recipes = db.list_recipes(conn)
    loved, hated = recipes[0], recipes[1]
    pid = db.create_plan(conn, "2026-01-05", [
        {"day": "Mon", "recipe_id": loved["id"], "servings": 2},
        {"day": "Tue", "recipe_id": hated["id"], "servings": 2},
    ], "heuristic")
    meals = db.get_plan(conn, pid)["meals"]
    db.save_feedback(conn, meals[0]["id"], loved["id"], 5, "yes", "amazing")
    db.save_feedback(conn, meals[1]["id"], hated["id"], 1, "no", "gross")

    assert db.get_recipe(conn, hated["id"])["archived"] == 1
    loved_count = 0
    for seed in range(10):
        pid = planner.plan_week(conn, dt.date(2026, 3, 2), use_ai=False, rng=random.Random(seed))
        ids = {m["recipe_id"] for m in db.get_plan(conn, pid)["meals"]}
        assert hated["id"] not in ids
        loved_count += loved["id"] in ids
    assert loved_count >= 8


def test_recently_planned_penalized(conn):
    r = db.list_recipes(conn)[0]
    stats = {r["id"]: {"last_week": "2026-11-16"}}
    s_recent = planner.score_recipe(r, stats, {}, dt.date(2026, 11, 23))
    s_fresh = planner.score_recipe(r, {}, {}, dt.date(2026, 11, 23))
    assert s_recent < s_fresh - 1


def test_swap_meal_changes_recipe(conn):
    pid = planner.plan_week(conn, dt.date(2026, 11, 23), use_ai=False, rng=random.Random(3))
    meal = db.get_plan(conn, pid)["meals"][0]
    msg = planner.swap_meal(conn, meal["id"], use_ai=False, rng=random.Random(4))
    assert "Swapped" in msg
    after = db.get_plan(conn, pid)
    ids = [m["recipe_id"] for m in after["meals"]]
    assert db.get_meal(conn, meal["id"])["recipe_id"] != meal["recipe_id"]
    assert len(ids) == len(set(ids))


def test_next_unplanned_week():
    today = dt.date(2026, 11, 18)
    assert planner.next_unplanned_week(None, today) == dt.date(2026, 11, 16)
    assert planner.next_unplanned_week("2026-11-16", today) == dt.date(2026, 11, 23)
    assert planner.next_unplanned_week("2026-01-05", today) == dt.date(2026, 11, 16)


def test_regenerate_keeps_locked(conn):
    pid = planner.plan_week(conn, dt.date(2026, 11, 23), use_ai=False, rng=random.Random(2))
    meals = db.get_plan(conn, pid)["meals"]
    db.update_meal(conn, meals[0]["id"], locked=1)
    before = {m["id"]: m["recipe_id"] for m in meals}
    msg = planner.regenerate(conn, pid, use_ai=False, rng=random.Random(99))
    after = db.get_plan(conn, pid)["meals"]
    assert {m["id"]: m["recipe_id"] for m in after}[meals[0]["id"]] == before[meals[0]["id"]]
    assert len({m["recipe_id"] for m in after}) == len(after)
    assert "kept 1 locked" in msg
    for m in after:
        db.update_meal(conn, m["id"], locked=1)
    assert "nothing to change" in planner.regenerate(conn, pid, use_ai=False)


def test_overlap_bonus_prefers_shared_produce():
    base = {"cuisine": "", "protein": "", "prep_minutes": 20, "servings": 2, "steps": []}
    fixed = dict(base, id=1, title="A", cuisine="x", protein="p1",
                 ingredients=[{"name": "cilantro", "category": "produce"}, {"name": "lime", "category": "produce"}])
    shares = dict(base, id=2, title="B", cuisine="y", protein="p2",
                  ingredients=[{"name": "cilantro", "category": "produce"}, {"name": "limes", "category": "produce"}])
    other = dict(base, id=3, title="C", cuisine="z", protein="p3",
                 ingredients=[{"name": "kale", "category": "produce"}])
    wins = 0
    for seed in range(30):
        pick = planner.heuristic_pick([shares, other], {}, {}, ["Tue"], dt.date(2026, 11, 23),
                                      rng=random.Random(seed), already=[fixed])
        wins += pick[0]["recipe_id"] == 2
    assert wins > 18


def test_cuisine_affinity_lifts_untried_recipes(conn):
    thai = [r for r in db.list_recipes(conn) if r["cuisine"] == "Thai"]
    assert thai
    # Rate a (new) Thai dish highly a few times
    rid = db.add_recipe(conn, {"title": "Pad Thai", "cuisine": "Thai", "protein": "shrimp",
                               "ingredients": [{"name": "rice noodles", "quantity": 8, "unit": "oz"}], "steps": []})
    pid = db.create_plan(conn, "2026-01-05", [{"day": d, "recipe_id": rid, "servings": 2} for d in ["Mon", "Tue", "Wed"]], "heuristic")
    for m in db.get_plan(conn, pid)["meals"]:
        db.save_feedback(conn, m["id"], rid, 5, "yes", "")
    aff = db.taste_affinity(conn)
    assert aff["cuisine"]["thai"]["score"] > 1
    week = dt.date(2026, 11, 23)
    with_aff = planner.score_recipe(thai[0], {}, {}, week, aff)
    without = planner.score_recipe(thai[0], {}, {}, week)
    assert with_aff > without + 0.5


def test_use_up_prefers_matching_recipes(conn):
    hits = 0
    for seed in range(10):
        pid = planner.plan_week(conn, dt.date(2026, 11, 23), use_ai=False, rng=random.Random(seed),
                                use_up=["coconut milk"])
        names = " ".join(i["name"] for m in db.get_plan(conn, pid)["meals"] for i in m["recipe"]["ingredients"])
        hits += "coconut milk" in names
    assert hits >= 9
