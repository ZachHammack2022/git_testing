import datetime as dt
import io

from recipe_planner import ai, db


def test_on_hand_expires_and_keeps_dates(conn):
    day1 = dt.date(2026, 11, 1)
    db.set_on_hand(conn, ["Spinach", "feta"], today=day1)
    db.set_on_hand(conn, ["spinach", "feta", "eggs"], today=day1 + dt.timedelta(days=3))
    items = {i["name"]: i["added"] for i in db.on_hand(conn, today=day1 + dt.timedelta(days=3))}
    assert items == {"spinach": "2026-11-01", "feta": "2026-11-01", "eggs": "2026-11-04"}
    # Spinach and feta fall off after ON_HAND_DAYS; eggs are newer.
    later = day1 + dt.timedelta(days=db.ON_HAND_DAYS + 1)
    assert [i["name"] for i in db.on_hand(conn, today=later)] == ["eggs"]
    # A new photo re-dates what it sees, and keeps the rest.
    db.set_on_hand(conn, ["feta"], today=later, replace=False)
    assert {i["name"] for i in db.on_hand(conn, today=later)} == {"eggs", "feta"}


def test_on_hand_is_unchecked_on_shopping_list_and_prefills_plan(client, app):
    with app.app_context():
        conn = db.connect(app.config["DB_FILE"])
        db.set_on_hand(conn, ["garlic"])
        conn.close()
    assert "Filled in from" in client.get("/").get_data(as_text=True)
    r = client.post("/plans", data={"week_start": "2026-11-23"})
    pid = int(r.headers["Location"].rsplit("/", 1)[1])
    page = client.get(f"/plans/{pid}/shopping").get_data(as_text=True)
    if 'data-key="garlic"' in page:  # only if this week's recipes use garlic
        line = page.split('data-key="garlic"')[1].split(">")[0]
        assert "checked" not in line


def test_kitchen_page_and_manual_edit(client):
    assert client.get("/kitchen").status_code == 200
    client.post("/kitchen", data={"on_hand": "spinach\nfeta, eggs"})
    page = client.get("/kitchen").get_data(as_text=True)
    assert "spinach, feta, eggs" in page


def test_scan_without_ai_explains(client):
    r = client.post("/kitchen/scan", data={"photos": (io.BytesIO(b"x"), "f.jpg", "image/jpeg")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "needs Claude" in r.get_data(as_text=True)


def test_scan_review_and_confirm(client, monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: True)
    seen = {}

    def fake_scan(images):
        seen["images"] = images
        return [{"name": "baby spinach", "kind": "fresh"}, {"name": "soy sauce", "kind": "staple"},
                {"name": "olive oil", "kind": "staple"}]

    monkeypatch.setattr(ai, "scan_kitchen", fake_scan)
    r = client.post("/kitchen/scan", data={"photos": [(io.BytesIO(b"\xff\xd8jpeg"), "a.jpg", "image/jpeg"),
                                                      (io.BytesIO(b"GIF"), "notes.txt", "text/plain")]},
                    content_type="multipart/form-data")
    page = r.get_data(as_text=True)
    assert seen["images"] == [(b"\xff\xd8jpeg", "image/jpeg")]
    assert "I spotted 3 things" in page
    assert "already a staple" in page  # olive oil is a default staple

    r = client.post("/kitchen/confirm", data={"name": ["baby spinach", "soy sauce", "olive oil"],
                                              "where-0": "fresh", "where-1": "staple", "where-2": "skip"},
                    follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "Added 1 to" in page and "baby spinach" in page and "soy sauce" in page


def test_scan_kitchen_parses_and_dedupes(monkeypatch):
    captured = {}

    def fake_call(prompt, schema, **kw):
        captured.update(kw)
        return {"items": [{"name": "Eggs", "kind": "fresh"}, {"name": "eggs", "kind": "fresh"},
                          {"name": "rice", "kind": "staple"}, {"name": " ", "kind": "fresh"}]}

    monkeypatch.setattr(ai, "_call", fake_call)
    out = ai.scan_kitchen([(b"img", "image/png")])
    assert out == [{"name": "eggs", "kind": "fresh"}, {"name": "rice", "kind": "staple"}]
    assert captured["images"] == [(b"img", "image/png")]
