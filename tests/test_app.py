import re

from recipe_planner import db


def _plan(client):
    r = client.post("/plans", data={"week_start": "2026-11-23"})
    assert r.status_code == 302
    return int(re.search(r"/plans/(\d+)", r.headers["Location"]).group(1))


def test_pages_render(client):
    for path in ["/", "/recipes", "/recipes/1", "/recipes/new", "/settings", "/history",
                 "/recipes?archived=1", "/recipes?q=thai", "/recipes/1/edit"]:
        assert client.get(path).status_code == 200, path


def test_full_weekly_loop(client, app):
    pid = _plan(client)
    page = client.get(f"/plans/{pid}").get_data(as_text=True)
    assert "Week of Nov 23, 2026" in page

    shop = client.get(f"/plans/{pid}/shopping").get_data(as_text=True)
    assert "Order from Whole Foods" in shop

    r = client.post(f"/plans/{pid}/order")
    assert r.status_code == 302
    oid = int(r.headers["Location"].rsplit("/", 1)[1])
    order_page = client.get(f"/orders/{oid}").get_data(as_text=True)
    assert "i=wholefoods" in order_page

    with app.app_context():
        conn = db.connect(app.config["DB_FILE"])
        order = db.get_order(conn, oid)
        assert order["items"] and not any(i["key"] == "salt" for i in order["items"])
        key = order["items"][0]["key"]
    r = client.post(f"/orders/{oid}/items/{key}", json={"added": True})
    assert r.get_json()["added"] == 1

    client.post(f"/orders/{oid}/picks", data={"key": key, "url": "https://www.amazon.com/dp/B000TEST", "label": "365 brand"})
    assert "B000TEST" in client.get(f"/orders/{oid}").get_data(as_text=True)
    assert client.post(f"/orders/{oid}/placed").status_code == 302

    conn = db.connect(app.config["DB_FILE"])
    plan = db.get_plan(conn, pid)
    form = {}
    for i, m in enumerate(plan["meals"]):
        form[f"rating-{m['id']}"] = "5" if i == 0 else "2"
        form[f"again-{m['id']}"] = "yes" if i == 0 else "no"
        form[f"notes-{m['id']}"] = "note"
    form["general_notes"] = "more fish please"
    assert client.post(f"/plans/{pid}/feedback", data=form).status_code == 302
    assert len(db.recent_feedback(conn)) == len(plan["meals"])
    assert "more fish please" in db.get_settings(conn)["notes"]
    retired = [m["recipe_id"] for m in plan["meals"][1:]]
    assert all(db.get_recipe(conn, rid)["archived"] for rid in retired)

    hist = client.get("/history").get_data(as_text=True)
    assert "What the planner has learned" in hist and "Retired" in hist

    # New order uses saved product pick when the item reappears
    assert db.product_picks(conn)[key]["url"].endswith("B000TEST")


def test_order_selection(client, app):
    pid = _plan(client)
    conn = db.connect(app.config["DB_FILE"])
    from recipe_planner import grocery
    items = grocery.build_shopping_list(db.get_plan(conn, pid)["meals"], db.pantry(conn))
    keep = [items[0]["key"], items[1]["key"]]
    r = client.post(f"/plans/{pid}/order", data={"buy": [""] + keep})
    oid = int(r.headers["Location"].rsplit("/", 1)[1])
    assert [i["key"] for i in db.get_order(conn, oid)["items"]] == keep


def test_rejects_non_amazon_pick(client, app):
    pid = _plan(client)
    oid = int(client.post(f"/plans/{pid}/order").headers["Location"].rsplit("/", 1)[1])
    r = client.post(f"/orders/{oid}/picks", data={"key": "x", "url": "https://evil.example.com"}, follow_redirects=True)
    assert "should be amazon.com" in r.get_data(as_text=True)


def test_meal_edit_swap_delete(client, app):
    pid = _plan(client)
    conn = db.connect(app.config["DB_FILE"])
    meal = db.get_plan(conn, pid)["meals"][0]
    client.post(f"/meals/{meal['id']}/update", data={"servings": "6"})
    assert db.get_meal(conn, meal["id"])["servings"] == 6
    client.post(f"/meals/{meal['id']}/swap", data={})
    assert db.get_meal(conn, meal["id"])["recipe_id"] != meal["recipe_id"]
    client.post(f"/meals/{meal['id']}/delete")
    assert db.get_meal(conn, meal["id"]) is None
    client.post(f"/plans/{pid}/meals", data={"day": "Sat", "recipe_id": "1"})
    assert any(m["day"] == "Sat" for m in db.get_plan(conn, pid)["meals"])


def test_recipe_crud(client, app):
    r = client.post("/recipes/new", data={
        "title": "Test Tacos", "cuisine": "Mexican", "protein": "beef", "servings": "2",
        "prep_minutes": "20", "ingredients": "1 lb ground beef\n8 corn tortillas\nsalt",
        "steps": "Cook beef\nMake tacos", "tags": "quick, test"})
    assert r.status_code == 302
    rid = int(r.headers["Location"].rsplit("/", 1)[1])
    conn = db.connect(app.config["DB_FILE"])
    rec = db.get_recipe(conn, rid)
    assert rec["ingredients"][0] == {"quantity": 1.0, "unit": "lb", "name": "ground beef", "category": "meat & seafood"}
    assert rec["tags"] == ["quick", "test"]
    client.post(f"/recipes/{rid}/archive")
    assert db.get_recipe(conn, rid)["archived"] == 1
    # missing ingredients is rejected
    r = client.post("/recipes/new", data={"title": "Empty", "ingredients": ""})
    assert r.status_code == 200 and "needs a title" in r.get_data(as_text=True)


def test_settings_save(client, app):
    client.post("/settings", data={"household_size": "4", "day-Mon": "on", "day-Sat": "on",
                                   "max_prep_minutes": "45", "dislikes": "olives",
                                   "pantry": "salt, pepper\nrice", "new_recipe_pct": "50"})
    conn = db.connect(app.config["DB_FILE"])
    s = db.get_settings(conn)
    assert s["household_size"] == 4 and s["cook_days"] == ["Mon", "Sat"] and s["dinners_per_week"] == 2
    assert s["new_recipe_ratio"] == 0.5
    assert set(db.pantry(conn)) == {"salt", "pepper", "rice"}


def test_remember_pantry(client, app):
    pid = _plan(client)
    conn = db.connect(app.config["DB_FILE"])
    from recipe_planner import grocery
    items = grocery.build_shopping_list(db.get_plan(conn, pid)["meals"], db.pantry(conn))
    client.post(f"/plans/{pid}/order", data={"buy": ["", items[0]["key"]], "remember_pantry": "on"})
    pantry = set(db.pantry(conn))
    assert items[1]["key"] in pantry and items[0]["key"] not in pantry


def test_always_buy_and_extras(client, app):
    conn = db.connect(app.config["DB_FILE"])
    db.save_settings(conn, {"always_buy": "1 gallon whole milk\n2 lb bananas"})
    pid = _plan(client)
    oid = int(client.post(f"/plans/{pid}/order").headers["Location"].rsplit("/", 1)[1])
    items = {i["key"]: i for i in db.get_order(conn, oid)["items"]}
    assert "banana" in items and items["banana"]["amount"] == "2 lb"
    assert items["banana"]["recipes"] == ["always buy"]

    client.post(f"/orders/{oid}/add", data={"items": "1 dozen eggs, sparkling water"})
    keys = [i["key"] for i in db.get_order(conn, oid)["items"]]
    assert "sparkling water" in keys and any("egg" in k for k in keys)
    client.post(f"/orders/{oid}/items/banana/remove")
    assert "banana" not in [i["key"] for i in db.get_order(conn, oid)["items"]]

    # opting out on the shopping page
    oid2 = int(client.post(f"/plans/{pid}/order", data={"include_always": ["off"]}).headers["Location"].rsplit("/", 1)[1])
    assert "banana" not in [i["key"] for i in db.get_order(conn, oid2)["items"]]


def test_cook_view_scales_and_rates(client, app):
    pid = _plan(client)
    conn = db.connect(app.config["DB_FILE"])
    meal = db.get_plan(conn, pid)["meals"][0]
    db.update_meal(conn, meal["id"], servings=meal["recipe"]["servings"] * 2)
    page = client.get(f"/meals/{meal['id']}/cook").get_data(as_text=True)
    from markupsafe import escape
    assert "scaled from" in page and str(escape(meal["recipe"]["title"])) in page
    client.post(f"/meals/{meal['id']}/feedback", data={"rating": "4", "again": "tweak", "notes": "more lime"})
    fb = db.recent_feedback(conn)[0]
    assert fb["rating"] == 4 and fb["make_again"] == "tweak" and fb["notes"] == "more lime"


def test_lock_and_regenerate_routes(client, app):
    pid = _plan(client)
    conn = db.connect(app.config["DB_FILE"])
    meal = db.get_plan(conn, pid)["meals"][0]
    client.post(f"/meals/{meal['id']}/lock")
    assert db.get_meal(conn, meal["id"])["locked"] == 1
    assert client.post(f"/plans/{pid}/regenerate").status_code == 302
    assert db.get_meal(conn, meal["id"])["recipe_id"] == meal["recipe_id"]
    assert "🔒" in client.get(f"/plans/{pid}").get_data(as_text=True)


def test_migration_adds_locked_column(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE plan_meals (id INTEGER PRIMARY KEY, plan_id INTEGER, day TEXT,"
                    " recipe_id INTEGER, servings INTEGER, cooked INTEGER DEFAULT 0);")
    c.commit(); c.close()
    conn = db.connect(path)
    db.init(conn)
    assert "locked" in {r["name"] for r in conn.execute("PRAGMA table_info(plan_meals)")}


def test_ics_backup_and_add_from_recipe(client, app):
    pid = _plan(client)
    ics = client.get(f"/plans/{pid}.ics")
    body = ics.get_data(as_text=True)
    assert ics.mimetype == "text/calendar" and body.count("BEGIN:VEVENT") == 5
    assert "DTSTART;VALUE=DATE:20261123" in body
    b = client.get("/backup.db")
    assert b.data[:16] == b"SQLite format 3\x00"
    page = client.get("/recipes/3").get_data(as_text=True)
    assert "Add to the week of" in page
    client.post(f"/plans/{pid}/meals", data={"day": "Sat", "recipe_id": "3"})
    conn = db.connect(app.config["DB_FILE"])
    assert any(m["day"] == "Sat" and m["recipe_id"] == 3 for m in db.get_plan(conn, pid)["meals"])
