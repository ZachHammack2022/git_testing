from recipe_planner import ai, db, prep


def _recipe(title, ingredients, steps=("Cook.",)):
    return {"id": hash(title) % 1000, "title": title, "servings": 2,
            "ingredients": [dict(zip(("name", "quantity", "unit", "category"), i)) for i in ingredients],
            "steps": list(steps)}


def _plan(*meals):
    return {"meals": [{"day": d, "recipe": r, "recipe_id": r["id"], "servings": 2, "cooked": 0}
                      for d, r in meals]}


TACOS = _recipe("Tacos", [("yellow onion", 1, "", "produce"), ("garlic", 2, "clove", "produce"),
                          ("avocado", 1, "", "produce"), ("ground beef", 1, "lb", "meat & seafood"),
                          ("black beans", 1, "can", "pantry")],
                ["Whisk lime juice and oil into a dressing.", "Cook beef."])
CURRY = _recipe("Curry", [("onion", 1, "", "produce"), ("jasmine rice", 1, "cup", "pantry"),
                          ("garlic", 3, "clove", "produce"), ("chicken thighs", 1, "lb", "meat & seafood")],
                ["Marinate the chicken in yogurt.", "Simmer."])
SALMON = _recipe("Salmon", [("salmon fillets", 2, "", "meat & seafood"), ("broccoli", 1, "head", "produce"),
                            ("brown rice", 1, "cup", "pantry")])


def test_builtin_prep_groups_and_skips_day_of_items():
    out = prep.builtin_plan(_plan(("Mon", TACOS), ("Tue", CURRY), ("Thu", SALMON)))
    early, late = out["sessions"]
    texts = [t["text"] for t in early["tasks"]]
    assert texts[0] == "Mince garlic (5 cloves)"  # shared across both meals comes first
    assert any(t.startswith("Chop yellow onion") for t in texts)
    assert any(t.startswith("Cook jasmine rice (1 cup)") for t in texts)
    assert not any("avocado" in t or "black beans" in t for t in texts)  # day-of / canned
    assert "Make the sauce for Tacos (Mon)" in texts
    assert any(t.startswith("Freeze salmon") for t in texts)  # Thursday fish
    assert [t["text"] for t in late["tasks"]] == ["Chop broccoli (1 head)", "Cook brown rice (1 cup)"]
    nights = {(n["day"], n["text"].split(" ")[0]) for n in out["night_before"]}
    assert ("Mon", "Start") in nights  # marinade for Tuesday's curry
    assert ("Wed", "Move") in nights   # thaw salmon for Thursday
    ids = [t["id"] for s in out["sessions"] for t in s["tasks"]]
    assert len(ids) == len(set(ids))


def test_prep_page_ticks_and_claude_version(client, app, monkeypatch):
    r = client.post("/plans", data={"week_start": "2026-11-23"})
    pid = int(r.headers["Location"].rsplit("/", 1)[1])
    page = client.get(f"/plans/{pid}/prep").get_data(as_text=True)
    assert "Prep plan" in page and "Built from your recipes" in page
    assert client.post(f"/plans/{pid}/prep/tick", json={"id": "abc", "done": True}).get_json()["done"] == 1

    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "prep_plan", lambda plan: {
        "sessions": [{"title": "Sunday", "subtitle": "45 min", "tasks": [
            {"text": "Roast all the vegetables", "detail": "425F", "days": ["Mon"]}]}],
        "night_before": [{"day": "Wed", "text": "Thaw fish"}]})
    client.post(f"/plans/{pid}/prep/ai")
    page = client.get(f"/plans/{pid}/prep").get_data(as_text=True)
    assert "Written by Claude" in page and "Roast all the vegetables" in page and "Thaw fish" in page
    assert "dinners changed" not in page

    # Changing a dinner flags the saved plan as stale.
    with app.app_context():
        conn = db.connect(app.config["DB_FILE"])
        meal = db.get_plan(conn, pid)["meals"][0]
        other = next(r for r in db.list_recipes(conn) if r["id"] != meal["recipe_id"])
        db.update_meal(conn, meal["id"], recipe_id=other["id"])
        conn.close()
    assert "dinners changed" in client.get(f"/plans/{pid}/prep").get_data(as_text=True)
    client.post(f"/plans/{pid}/prep/reset")
    assert "Built from your recipes" in client.get(f"/plans/{pid}/prep").get_data(as_text=True)
    # Ticks survive switching versions.
    assert db.get_prep(db.connect(app.config["DB_FILE"]), pid)["done"] == ["abc"]
