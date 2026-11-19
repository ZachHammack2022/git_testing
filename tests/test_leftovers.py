import datetime as dt

from recipe_planner import db, grocery, leftovers

TODAY = dt.date(2026, 11, 20)


def _items(*rows):
    meals = [{"servings": 4, "recipe": {"title": "T", "servings": 4, "ingredients": [
        {"name": n, "quantity": q, "unit": u, "category": c} for n, q, u, c in rows]}}]
    return grocery.build_shopping_list(meals)


def by_name(res):
    return {r["name"]: r for r in res}


def test_fraction_of_a_bunch_is_left():
    res = by_name(leftovers.estimate(_items(("fresh mint", 0.25, "bunch", "produce")), TODAY))
    assert res["fresh mint"]["amount"] == "about 3/4 bunch"
    assert res["fresh mint"]["use_by"] == "2026-11-27"          # herbs keep a week


def test_whole_items_leave_nothing():
    assert leftovers.estimate(_items(("lemon", 2, "", "produce"), ("limes", 1, "", "produce")), TODAY) == []


def test_pack_size_remainder_for_dairy():
    res = by_name(leftovers.estimate(_items(("sour cream", 0.75, "cup", "dairy & eggs")), TODAY))
    assert res["sour cream"]["amount"].startswith("about 1")     # 2-cup tub minus 3/4 cup = 1 1/4 cup
    assert res["sour cream"]["use_by"] == "2026-12-04"


def test_nearly_used_pack_is_ignored():
    assert leftovers.estimate(_items(("sour cream", 1.9, "cup", "dairy & eggs")), TODAY) == []


def test_pantry_meat_and_spices_are_skipped():
    rows = [("ground beef", 1.5, "lb", "meat & seafood"), ("cumin", 1, "tsp", "spices"),
            ("rice", 0.5, "cup", "pantry"), ("garlic", 3, "clove", "produce")]
    assert leftovers.estimate(_items(*rows), TODAY) == []


def test_leftovers_expire_on_their_own_date(conn):
    db.add_leftovers(conn, [{"name": "Mint", "amount": "about 3/4 bunch", "use_by": "2026-11-27"}], TODAY)
    assert [i["name"] for i in db.on_hand(conn, TODAY + dt.timedelta(days=6))] == ["mint"]
    assert db.on_hand(conn, TODAY + dt.timedelta(days=8)) == []           # past use-by
    # Typed-in food still follows the 10-day rule, and editing the list keeps leftover details.
    db.set_on_hand(conn, ["mint", "spinach"], TODAY)
    kept = {i["name"]: i for i in db.on_hand(conn, TODAY)}
    assert kept["mint"]["amount"] == "about 3/4 bunch" and "use_by" not in kept["spinach"]


def test_placing_an_order_records_leftovers_and_home_shows_them(client):
    conn = db.connect(client.application.config["DB_FILE"])
    db.init(conn)
    rid = db.add_recipe(conn, {"title": "Mint Test", "servings": 4, "prep_minutes": 10, "protein": "vegetarian",
                               "ingredients": [{"name": "fresh mint", "quantity": 0.25, "unit": "bunch", "category": "produce"},
                                               {"name": "rice", "quantity": 1, "unit": "cup", "category": "pantry"}],
                               "steps": ["Chop."]})
    pid = int(client.post("/plans", data={"week_start": "2026-11-16"}).headers["Location"].rsplit("/", 1)[1])
    client.post(f"/plans/{pid}/meals", data={"day": "Sat", "recipe_id": str(rid)})
    oid = int(client.post(f"/plans/{pid}/order").headers["Location"].rsplit("/", 1)[1])
    page = client.post(f"/orders/{oid}/placed", data={"total": ""}, follow_redirects=True).get_data(as_text=True)
    assert "leftover" in page and "fresh mint" in page
    assert any(i["name"] == "fresh mint" for i in db.on_hand(conn))
    assert "Use up soon" in client.get("/").get_data(as_text=True)
    # placing again doesn't duplicate anything
    n = len(db.on_hand(conn))
    client.post(f"/orders/{oid}/placed", data={"total": "50"})
    assert len(db.on_hand(conn)) == n


def test_recipe_search_matches_ingredients(client):
    page = client.get("/recipes?q=couscous").get_data(as_text=True)
    assert "Moroccan Chicken" in page
