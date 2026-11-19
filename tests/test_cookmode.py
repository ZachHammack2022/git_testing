import pytest

from recipe_planner import cookmode


@pytest.mark.parametrize("step,expected", [
    ("Simmer covered 20 min.", [1200]),
    ("Roast 15 min, then 5 minutes more.", [900, 300]),
    ("Cook 10-12 minutes until golden.", [720]),
    ("Braise for 1 1/2 hours.", [5400]),
    ("Rest 30 seconds.", [30]),
    ("Chill 2 hrs", [7200]),
    ("Heat oven to 425°F.", []),
    ("Add 2 cups of rice and 1/2 cup water.", []),
    ("Cook 5 min, 5 min", [300]),
])
def test_find_timers(step, expected):
    assert [t["seconds"] for t in cookmode.find_timers(step)] == expected


def test_labels():
    labels = [t["label"] for t in cookmode.find_timers("Wait 90 seconds, then 2 hours, then 45 min")]
    assert labels == ["1 min 30 sec", "2 hr", "45 min"]


def _meal_with_timer(client):
    from recipe_planner import db
    conn = db.connect(client.application.config["DB_FILE"])
    db.init(conn)
    rid = db.add_recipe(conn, {"title": "Timer Test", "servings": 4, "prep_minutes": 20, "protein": "vegetarian",
                               "ingredients": [{"name": "rice", "quantity": 1, "unit": "cup", "category": "pantry"}],
                               "steps": ["Boil water.", "Simmer covered 20 min."]})
    pid = int(client.post("/plans", data={"week_start": "2026-11-16"}).headers["Location"].rsplit("/", 1)[1])
    client.post(f"/plans/{pid}/meals", data={"day": "Sat", "recipe_id": str(rid)})
    return conn.execute("SELECT id FROM plan_meals WHERE plan_id=? AND recipe_id=?", (pid, rid)).fetchone()["id"]


def test_cook_page_has_timers_and_scaler(client):
    meal = _meal_with_timer(client)
    page = client.get(f"/meals/{meal}/cook").get_data(as_text=True)
    assert page.count('class="timerbtn"') == 1 and "Start 20 min timer" in page
    assert "?servings=" in page
    bigger = client.get(f"/meals/{meal}/cook?servings=12").get_data(as_text=True)
    assert "12 servings" in bigger and "Reset" in bigger
    # absurd values are clamped rather than rejected
    assert "40 servings" in client.get(f"/meals/{meal}/cook?servings=999").get_data(as_text=True)
