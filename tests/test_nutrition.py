import pytest

from recipe_planner import db, nutrition, planner, usda

PER100 = {"calories": 100.0, "protein": 10.0, "carbs": 5.0, "fat": 2.0, "fiber": 1.0, "sodium": 50.0}


def fake(name, unit=""):
    return None if "mystery" in name else {"fdc_id": 1, "description": f"fake {name}", "per100": dict(PER100)}


def ing(name, q, unit="", cat="produce"):
    return {"name": name, "quantity": q, "unit": unit, "category": cat}


@pytest.mark.parametrize("line,expected", [
    (ing("chicken thighs", 1, "lb", "meat & seafood"), 453.6),
    (ing("olive oil", 2, "tbsp", "pantry"), 27.2),
    (ing("jasmine rice", 1, "cup", "pantry"), 189.3),
    (ing("eggs", 3, "", "dairy & eggs"), 150),
    (ing("garlic", 4, "clove"), 12),
    (ing("black beans", 1, "can", "pantry"), 400),
    (ing("lemon", 2), 120),
    (ing("salt", 1, "tsp", "spices"), 0),          # amounts of 0 or unknown give 0
])
def test_grams(line, expected):
    got = nutrition.grams(line)
    assert got == pytest.approx(expected, rel=0.03) if expected else got >= 0


def test_search_queries_and_matching():
    assert usda.search_query("chopped fresh jasmine rice") == "rice white long-grain regular raw"
    assert usda.search_query("black beans", "can") == "black beans canned"
    assert usda.search_query("linguine") == "pasta dry enriched"
    foods = [
        {"fdcId": 1, "description": "Chicken, thigh, cooked, roasted", "foodNutrients": [{"nutrientId": 1008, "value": 200}]},
        {"fdcId": 2, "description": "Chicken, thigh, meat only, raw", "foodNutrients": [{"nutrientId": 1008, "value": 121}]},
        {"fdcId": 3, "description": "Chicken thigh, restaurant", "foodNutrients": [{"nutrientId": 1008, "value": 300}]},
    ]
    assert usda.pick_best(foods, "chicken thigh meat only raw")["fdcId"] == 2


def test_foundation_foods_use_atwater_energy():
    food = {"foodNutrients": [{"nutrientId": 2047, "value": 130.0}, {"nutrientId": 1003, "value": 20.0}]}
    assert usda.per100(food)["calories"] == 130.0 and usda.per100(food)["protein"] == 20.0
    assert usda.per100({"foodNutrients": []}) is None


def test_compute_per_serving_and_missing(conn):
    recipe = {"servings": 2, "ingredients": [ing("chicken thighs", 1, "lb", "meat & seafood"),   # 453.6 g
                                             ing("mystery spice", 1, "tbsp", "spices"),
                                             ing("salt", 1, "tsp", "spices")]}
    res = nutrition.compute(conn, recipe, fake)
    assert res["per_serving"]["calories"] == round(453.6 * 1.0 / 2)       # 100 kcal/100 g, split over 2
    assert res["per_serving"]["protein"] == pytest.approx(22.7, abs=0.1)
    assert (res["matched"], res["total"], res["missing"]) == (1, 2, ["mystery spice"])  # salt is ignored


def test_each_ingredient_is_looked_up_once(conn):
    calls = []

    def counting(name, unit=""):
        calls.append(name)
        return fake(name)
    r1 = {"servings": 2, "ingredients": [ing("garlic", 2, "clove"), ing("onion", 1)]}
    r2 = {"servings": 4, "ingredients": [ing("garlic", 6, "clove")]}
    nutrition.compute(conn, r1, counting)
    nutrition.compute(conn, r2, counting)
    assert sorted(calls) == ["garlic", "onion"]


def test_rate_limit_keeps_progress_and_caches_nothing_wrong(conn):
    seen = []

    def limited(name, unit=""):
        seen.append(name)
        if name == "onion":
            raise usda.RateLimited()
        return fake(name)
    recipe = {"servings": 2, "ingredients": [ing("garlic", 2, "clove"), ing("onion", 1), ing("lemon", 1)]}
    with pytest.raises(usda.RateLimited):
        nutrition.compute(conn, recipe, limited)
    assert nutrition.compute(conn, {"servings": 1, "ingredients": [ing("garlic", 1, "clove")]}, limited)["matched"] == 1
    assert seen.count("garlic") == 1                                     # garlic was saved before the limit hit
    assert conn.execute("SELECT COUNT(*) FROM food_cache WHERE name LIKE 'onion%'").fetchone()[0] == 0


def test_saved_nutrition_goes_stale_when_ingredients_change(conn):
    rid = db.add_recipe(conn, {"title": "T", "servings": 2, "ingredients": [ing("garlic", 2, "clove")], "steps": ["x"]})
    nutrition.calculate(conn, rid, fake)
    assert nutrition.current(db.get_recipe(conn, rid)) is not None
    r = db.get_recipe(conn, rid)
    r["ingredients"].append(ing("onion", 1))
    db.update_recipe(conn, rid, r)
    assert nutrition.current(db.get_recipe(conn, rid)) is None


def test_goal_adjust():
    nut = {"per_serving": {"protein": 40, "calories": 500}}
    assert nutrition.goal_adjust(None, {"min_protein": 30}) == 0
    assert nutrition.goal_adjust(nut, {}) == 0
    assert nutrition.goal_adjust(nut, {"min_protein": 30}) > 0
    assert nutrition.goal_adjust(nut, {"min_protein": 80}) < -1
    assert nutrition.goal_adjust(nut, {"max_calories": 300}) < -1


def test_goals_steer_scoring(conn):
    hi = {"id": 1, "cuisine": "x", "protein": "chicken", "prep_minutes": 20, "servings": 2,
          "ingredients": [ing("chicken", 1, "lb", "meat & seafood")]}
    nutrition_hi = nutrition.compute(conn, hi, fake)            # ~227 kcal / 22.7 g protein per serving
    lo = dict(hi, id=2, ingredients=[ing("lettuce", 1)])
    nutrition_lo = nutrition.compute(conn, lo, lambda n, u="": {"fdc_id": 2, "description": "x", "per100": dict(PER100, protein=1.0)})
    settings = {"min_protein": 20}
    import datetime as dt
    s_hi = planner.score_recipe(dict(hi, nutrition=nutrition_hi), {}, settings, dt.date(2026, 11, 16))
    s_lo = planner.score_recipe(dict(lo, nutrition=nutrition_lo), {}, settings, dt.date(2026, 11, 16))
    assert s_hi > s_lo


def test_background_fill_and_pages(client, monkeypatch):
    monkeypatch.setattr(usda, "lookup", fake)
    app = client.application
    conn = db.connect(app.config["DB_FILE"])
    assert nutrition.start_fill(app.config["DB_FILE"], wait=True)
    assert nutrition.fill_status()["state"] == "done"
    assert all(nutrition.current(r) for r in db.list_recipes(conn))
    page = client.get("/recipes/1").get_data(as_text=True)
    assert "Per serving, estimated from USDA" in page and "Recalculate nutrition" in page
    client.post("/plans", data={"week_start": "2026-11-16"})
    plan = client.get("/plans/1").get_data(as_text=True)
    assert "Per dinner:" in plan and "dinners with nutrition data" in plan
    status = client.get("/nutrition/status").get_json()
    assert status["have"] == status["recipes_total"] > 0


def test_recipe_button_reports_rate_limit(client, monkeypatch):
    def limited(name, unit=""):
        raise usda.RateLimited()
    monkeypatch.setattr(usda, "lookup", limited)
    page = client.post("/recipes/1/nutrition", follow_redirects=True).get_data(as_text=True)
    assert "rate-limiting" in page
