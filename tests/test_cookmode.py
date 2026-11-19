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


def _meal(client):
    r = client.post("/plans", data={"week_start": "2026-11-16"})
    pid = int(r.headers["Location"].rsplit("/", 1)[1])
    return pid


def test_cook_page_has_timers_and_scaler(client):
    pid = _meal(client)
    from recipe_planner import db
    conn = db.connect(client.application.config["DB_FILE"])
    meal = conn.execute("SELECT id FROM plan_meals WHERE plan_id=?", (pid,)).fetchone()["id"]
    page = client.get(f"/meals/{meal}/cook").get_data(as_text=True)
    assert 'class="timerbtn"' in page          # every starter recipe has a timed step
    assert "?servings=" in page
    bigger = client.get(f"/meals/{meal}/cook?servings=12").get_data(as_text=True)
    assert "12 servings" in bigger and "Reset" in bigger
    # absurd values are clamped rather than rejected
    assert "40 servings" in client.get(f"/meals/{meal}/cook?servings=999").get_data(as_text=True)
