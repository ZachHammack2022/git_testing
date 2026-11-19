import random

from recipe_planner import costing, db, grocery, planner


def _recipe(i, title, cost_item, protein="vegetarian"):
    name, qty, unit, cat = cost_item
    return {"id": i, "title": title, "cuisine": f"c{i}", "protein": protein, "prep_minutes": 30, "servings": 4,
            "tags": [], "archived": 0,
            "ingredients": [{"name": name, "quantity": qty, "unit": unit, "category": cat}]}


def _items(*rows):
    meals = [{"servings": 4, "recipe": {"title": "T", "servings": 4, "ingredients": [
        {"name": n, "quantity": q, "unit": u, "category": c} for n, q, u, c in rows]}}]
    return grocery.build_shopping_list(meals)


def test_meat_is_priced_by_weight():
    cheap = costing.item_price(_items(("chicken thighs", 1, "lb", "meat & seafood"))[0])
    dear = costing.item_price(_items(("salmon fillets", 2, "lb", "meat & seafood"))[0])
    assert cheap == 4.5 and dear == 28.0


def test_learned_price_beats_default():
    item = _items(("feta cheese", 4, "oz", "dairy & eggs"))[0]
    assert costing.item_price(item) == 4.0
    assert costing.item_price(item, {item["key"]: 6.49}) == 6.49


def test_pantry_items_cost_nothing_and_shared_items_count_once():
    a = {"recipe": {"title": "A", "servings": 4, "ingredients": [{"name": "lime", "quantity": 1, "unit": "", "category": "produce"}]}, "servings": 4}
    b = {"recipe": {"title": "B", "servings": 4, "ingredients": [{"name": "lime", "quantity": 1, "unit": "", "category": "produce"}]}, "servings": 4}
    assert costing.cost_of([a, b], None, []) == 2.5            # one lime purchase covers both
    assert costing.cost_of([a], None, ["lime"]) == 0.0         # already have it


def test_budget_adjust_shape():
    assert planner.budget_adjust(None, 20) == 0
    assert planner.budget_adjust(10, 20) > 0                    # cheaper than the share: small bonus
    assert planner.budget_adjust(40, 20) < -1                   # twice the share: clear penalty
    assert planner.budget_adjust(500, 20) == -3.0               # capped


def test_tight_budget_steers_the_picker_to_cheaper_recipes():
    recipes = [_recipe(1, "Steak", ("steak", 2, "lb", "meat & seafood"), "beef"),
               _recipe(2, "Salmon", ("salmon", 2, "lb", "meat & seafood"), "fish"),
               _recipe(3, "Beans", ("black beans", 2, "can", "pantry")),
               _recipe(4, "Rice bowl", ("rice", 2, "cup", "pantry"))]
    costs = costing.recipe_costs(recipes, 4, None, [])
    assert costs[1] > 20 and costs[3] < 10
    days = ["Mon", "Tue"]
    start = planner.upcoming_monday()
    tight = planner.heuristic_pick(recipes, {}, {"weekly_budget": 20}, days, start, rng=random.Random(1), costs=costs)
    assert {p["recipe_id"] for p in tight} == {3, 4}


def test_prices_are_remembered(conn):
    db.save_prices(conn, {"feta cheese": 5.99, "bad": 0})
    assert db.price_book(conn) == {"feta cheese": 5.99}
    db.save_prices(conn, {"feta cheese": 6.49})
    assert db.price_book(conn)["feta cheese"] == 6.49


def test_plan_page_shows_budget_and_warns_when_over(client):
    client.post("/plans", data={"week_start": "2026-11-16"})
    assert "Set a weekly budget" in client.get("/plans/1").get_data(as_text=True)
    client.post("/settings", data={"weekly_budget": "5", "household_size": "2", "max_prep_minutes": "60",
                                   "new_recipe_pct": "0", "day-Mon": "on", "day-Tue": "on"})
    page = client.get("/plans/1").get_data(as_text=True)
    assert "of your $5 budget" in page and "Over by about" in page and "priciest dinner" in page
    client.post("/settings", data={"weekly_budget": "5000", "household_size": "2", "max_prep_minutes": "60",
                                   "new_recipe_pct": "0", "day-Mon": "on", "day-Tue": "on"})
    assert "Under budget by about" in client.get("/plans/1").get_data(as_text=True)
