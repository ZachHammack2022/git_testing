import re

from recipe_planner import db


def _plan(client):
    r = client.post("/plans", data={"week_start": "2026-11-23"})
    assert r.status_code == 302
    return int(re.search(r"/plans/(\d+)", r.headers["Location"]).group(1))


def test_pages_render(client):
    for path in ["/", "/recipes", "/recipes/1", "/recipes/new", "/settings", "/history",
                 "/recipes?archived=1", "/recipes?q=thai", "/recipes/1/edit", "/recipes/discover"]:
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


def test_tonight_card(client, app):
    import datetime as dt
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    conn = db.connect(app.config["DB_FILE"])
    db.create_plan(conn, monday.isoformat(), [{"day": db.DAYS[today.weekday()], "recipe_id": 1, "servings": 2}], "heuristic")
    page = client.get("/").get_data(as_text=True)
    assert "Tonight" in page and "Start cooking" in page


def test_repeat_plan(client, app):
    pid = _plan(client)
    r = client.post(f"/plans/{pid}/repeat")
    new_id = int(r.headers["Location"].rsplit("/", 1)[1])
    conn = db.connect(app.config["DB_FILE"])
    old, new = db.get_plan(conn, pid), db.get_plan(conn, new_id)
    from recipe_planner import planner
    assert new["week_start"] == planner.next_unplanned_week("2026-11-23").isoformat()
    assert [m["recipe_id"] for m in new["meals"]] == [m["recipe_id"] for m in old["meals"]]


def test_cook_shows_past_notes(client, app):
    conn = db.connect(app.config["DB_FILE"])
    p1 = db.create_plan(conn, "2026-11-09", [{"day": "Mon", "recipe_id": 2, "servings": 2}], "heuristic")
    db.save_feedback(conn, db.get_plan(conn, p1)["meals"][0]["id"], 2, 3, "tweak", "needs more ginger")
    p2 = db.create_plan(conn, "2026-11-16", [{"day": "Mon", "recipe_id": 2, "servings": 2}], "heuristic")
    m2 = db.get_plan(conn, p2)["meals"][0]["id"]
    page = client.get(f"/meals/{m2}/cook").get_data(as_text=True)
    assert "Your notes from last time" in page and "needs more ginger" in page


def test_settings_day_themes(client, app):
    client.post("/settings", data={"day-Tue": "on", "theme-Tue": "tacos", "theme-Wed": " "})
    s = db.get_settings(db.connect(app.config["DB_FILE"]))
    assert s["day_themes"] == {"Tue": "tacos"}
    assert 'value="tacos"' in client.get("/settings").get_data(as_text=True)


def test_leftovers_scale_shopping(client, app):
    from recipe_planner import grocery
    conn = db.connect(app.config["DB_FILE"])
    pid = db.create_plan(conn, "2026-11-23", [{"day": "Mon", "recipe_id": 1, "servings": 2}], "heuristic")
    mid = db.get_plan(conn, pid)["meals"][0]["id"]
    before = {i["key"]: i["amount"] for i in grocery.build_shopping_list(db.get_plan(conn, pid)["meals"])}
    client.post(f"/meals/{mid}/leftovers")
    plan = db.get_plan(conn, pid)
    assert plan["meals"][0]["leftovers"] == 2 and plan["meals"][0]["total_servings"] == 4
    after = {i["key"]: i["amount"] for i in grocery.build_shopping_list(plan["meals"])}
    assert after["chicken thigh"] == "2 lb" and before["chicken thigh"] == "1 lb"
    assert "for leftovers" in client.get(f"/meals/{mid}/cook").get_data(as_text=True)
    client.post(f"/meals/{mid}/leftovers")
    assert db.get_meal(conn, mid)["leftovers"] == 0


def test_spending(client, app):
    pid = _plan(client)
    oid = int(client.post(f"/plans/{pid}/order").headers["Location"].rsplit("/", 1)[1])
    client.post(f"/orders/{oid}/placed", data={"total": "$150.00"})
    conn = db.connect(app.config["DB_FILE"])
    assert db.get_order(conn, oid)["total"] == 150.0
    s = db.spending_summary(conn)
    assert s["avg_week"] == 150 and s["avg_dinner"] == 30
    hist = client.get("/history").get_data(as_text=True)
    assert "$150" in hist and "$30.00" in hist
    r = client.post(f"/orders/{oid}/placed", data={"total": "abc"}, follow_redirects=True)
    assert "should be a number" in r.get_data(as_text=True)
    client.post(f"/orders/{oid}/placed", data={"total": "160.5"})
    assert db.get_order(conn, oid)["total"] == 160.5


def test_password_protection(tmp_path, monkeypatch):
    import base64
    from recipe_planner.app import create_app
    monkeypatch.setenv("RECIPE_PLANNER_PASSWORD", "s3cret")
    c = create_app(str(tmp_path / "pw.db")).test_client()
    assert c.get("/").status_code == 401
    assert c.get("/static/style.css").status_code == 200
    bad = base64.b64encode(b"me:wrong").decode()
    assert c.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = base64.b64encode(b"me:s3cret").decode()
    assert c.get("/", headers={"Authorization": f"Basic {good}"}).status_code == 200


def test_manifest(client):
    m = client.get("/manifest.webmanifest").get_json()
    assert m["short_name"] == "Meals" and m["icons"]


def test_recipe_sort_and_filter(client, app):
    conn = db.connect(app.config["DB_FILE"])
    pid = db.create_plan(conn, "2026-11-09", [{"day": "Mon", "recipe_id": 5, "servings": 2}], "heuristic")
    db.save_feedback(conn, db.get_plan(conn, pid)["meals"][0]["id"], 5, 5, "yes", "")
    from markupsafe import escape
    top = client.get("/recipes?sort=rating").get_data(as_text=True)
    first_title = str(escape(db.get_recipe(conn, 5)["title"]))
    assert top.index(first_title) < top.index("Mushroom Risotto")
    untried = client.get("/recipes?untried=1").get_data(as_text=True)
    assert first_title not in untried
    quick = client.get("/recipes?sort=quick").get_data(as_text=True)
    assert "Pesto Gnocchi" in quick


def test_free_port_skips_busy():
    import socket
    from recipe_planner.__main__ import free_port, port_in_use
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    busy = s.getsockname()[1]
    try:
        assert port_in_use(busy)
        assert free_port(busy) != busy
    finally:
        s.close()


def test_product_preview_choose_and_estimate(client, app):
    from recipe_planner import ordering

    pid = _plan(client)
    oid = int(client.post(f"/plans/{pid}/order").headers["Location"].rsplit("/", 1)[1])
    conn = db.connect(app.config["DB_FILE"])
    order = db.get_order(conn, oid)
    a, b = order["items"][0], order["items"][1]
    a["options"] = [{"asin": "B0AAAAAAA1", "title": "First <b>choice</b>", "image": "https://m.media-amazon.com/x.jpg",
                     "price": 2.5, "unit_price": "$0.50/Ounce"},
                    {"asin": "B0AAAAAAA2", "title": "Organic it's better", "image": "", "price": 4.0, "unit_price": ""}]
    b["options"] = [{"asin": "B0BBBBBBB1", "title": "Only one", "image": "", "price": None, "unit_price": ""}]
    db.save_order_items(conn, oid, order["items"])

    page = client.get(f"/orders/{oid}").get_data(as_text=True)
    assert "≈ $2.50" in page and "for 1 of" in page
    assert "<b>choice" not in page  # option data is escaped
    assert "Look up the rest (" in page

    r = client.post(f"/orders/{oid}/items/{a['key']}/choose", json={"asin": "B0AAAAAAA2"})
    d = r.get_json()
    assert d["label"] == "Organic it's better" and d["estimate"]["total"] == 4.0
    item = db.get_order(conn, oid)["items"][0]
    assert item["has_pick"] and "B0AAAAAAA2" in item["url"] and "almBrandId" in item["url"]
    assert ordering.chosen_option(item)["asin"] == "B0AAAAAAA2"
    assert db.product_picks(conn)[a["key"]]["label"] == "Organic it's better"
    # Unknown product id is refused.
    assert client.post(f"/orders/{oid}/items/{a['key']}/choose", json={"asin": "B0NOPE0000"}).status_code == 400


def _order_id(client):
    pid = _plan(client)
    return int(client.post(f"/plans/{pid}/order").headers["Location"].rsplit("/", 1)[1])


def test_preview_waits_for_running_cart_check(client, app, monkeypatch, tmp_path):
    import os
    import subprocess

    from recipe_planner import autocart

    monkeypatch.setenv("RECIPE_DB", app.config["DB_FILE"])
    monkeypatch.setattr(autocart, "playwright_available", lambda: True)
    started = []

    class FakeProc:
        pid = os.getpid()  # a live process

        def __init__(self, args, **kw):
            started.append(args)

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    oid = _order_id(client)
    # The page's automatic cart check is running (a live process).
    autocart.write_status(oid, state="syncing", pid=os.getpid())
    page = client.post(f"/orders/{oid}/preview", follow_redirects=True).get_data(as_text=True)
    assert started == []  # not dropped, not clashing: queued
    assert "starts as soon as the cart check finishes" in page
    assert "busy" not in page

    # The check ends; the next status poll starts the preview.
    autocart.write_status(oid, state="idle", pid=None)
    st = client.get(f"/orders/{oid}/autofill").get_json()["status"]
    assert started and "--preview" in started[0] and "--show" not in started[0]
    assert st["state"] == "previewing" and not st.get("pending_preview")

    # That process dies without reporting back: shown as finished, not stuck.
    autocart.write_status(oid, pid=999999)
    st = client.get(f"/orders/{oid}/autofill").get_json()
    assert st["status"]["state"] == "idle" and not st["active"]
    page = client.get(f"/orders/{oid}").get_data(as_text=True)
    assert 'id="preview-btn" >' in page or 'id="preview-btn">' in page  # enabled again


def test_preview_outcome_is_reported(client, app, monkeypatch):
    from recipe_planner import autocart

    monkeypatch.setenv("RECIPE_DB", app.config["DB_FILE"])
    monkeypatch.setattr(autocart, "playwright_available", lambda: True)
    oid = _order_id(client)
    autocart.write_status(oid, state="idle", preview={"ok": True, "found": 3, "looked_up": 4, "at": 1})
    assert "Found products for 3 of 4 items." in client.get(f"/orders/{oid}").get_data(as_text=True)
    autocart.write_status(oid, preview={"ok": False, "found": 0, "looked_up": 4, "at": 1, "shot": "preview.png",
                                        "error": "I couldn't read any products"})
    page = client.get(f"/orders/{oid}").get_data(as_text=True)
    assert "read any products" in page and "Preview with the browser showing" in page
    assert "/autofill/shot/preview.png" in page


def test_saved_product_is_always_shown_first(client, app):
    from recipe_planner import ordering

    oid = _order_id(client)
    conn = db.connect(app.config["DB_FILE"])
    order = db.get_order(conn, oid)
    a, b, c = order["items"][:3]
    # a: your pick wasn't among the results (its page couldn't be read): still shown, first.
    a.update(has_pick=True, url="https://www.amazon.com/dp/B0PICK0001?x=1",
             pick_label="auto-picked: Gotham Greens Butterhead",
             options=[{"asin": "B0OTHER001", "title": "Other lettuce", "image": "", "price": 2.0, "unit_price": ""}])
    b["options"] = []          # searched, nothing found
    c.pop("options", None)     # never looked up
    db.save_order_items(conn, oid, order["items"])

    item = db.get_order(conn, oid)["items"][0]
    shown = ordering.display_options(item)
    assert [o["asin"] for o in shown] == ["B0PICK0001", "B0OTHER001"]
    assert shown[0]["pick"] and shown[0]["title"] == "Gotham Greens Butterhead"
    assert ordering.chosen_option(item)["asin"] == "B0PICK0001"

    page = client.get(f"/orders/{oid}").get_data(as_text=True)
    assert "No Whole Foods results for" in page
    assert "Not looked up yet" in page
    assert "Look up the rest (" in page and 'name="refresh"' not in page
    r = client.post(f"/orders/{oid}/items/{a['key']}/choose", json={"asin": "B0PICK0001"})
    assert r.status_code == 200
