import datetime as dt
import random
import time
import urllib.parse

import pytest

from recipe_planner import busy, db, google, notify, planner, reminders

TZ = dt.datetime.now().astimezone().tzinfo
MON = dt.date(2026, 11, 16)


def at(day, h, m=0):
    return dt.datetime.combine(day, dt.time(h, m), TZ)


class FakeGoogle:
    """Stands in for Google's OAuth and Calendar endpoints."""

    def __init__(self):
        self.items, self.events, self.calls, self.refresh_error = [], {}, [], None

    def __call__(self, method, url, *, form=None, body=None, token=None, timeout=20):
        self.calls.append((method, url.split("?")[0].replace(google.API, "")))
        if url == google.TOKEN_URL:
            if form["grant_type"] == "authorization_code":
                return 200, {"access_token": "acc1", "refresh_token": "ref1", "expires_in": 3600}
            if self.refresh_error:
                return 400, {"error": self.refresh_error}
            return 200, {"access_token": "acc2", "expires_in": 3600}
        assert token, "calendar calls must be authorised"
        path = url[len(google.API):].split("?")[0]
        if method == "GET" and path == "/events":
            return 200, {"items": self.items}
        if method == "POST" and path == "/events":
            self.counter = getattr(self, "counter", 0) + 1
            eid = f"ev{self.counter}"
            self.events[eid] = body
            return 200, {"id": eid}
        eid = path.rsplit("/", 1)[-1]
        if eid not in self.events:
            return 404, {}
        if method == "PATCH":
            self.events[eid] = body
            return 200, {"id": eid}
        if method == "DELETE":
            del self.events[eid]
            return 204, {}
        raise AssertionError(path)


@pytest.fixture
def gapi(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    fake = FakeGoogle()
    monkeypatch.setattr(google, "_request", fake)
    busy._cache.clear()
    return fake


def sign_in(conn):
    db.save_settings(conn, {"google": {"refresh_token": "ref", "access_token": "acc", "expires_at": time.time() + 3000}})


def ev(title, start, end, **extra):
    item = {"summary": title, "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
    item.update(extra)
    return item


# ---------- sign-in ----------

def test_sign_in_flow(client, gapi):
    r = client.get("/google/connect")
    assert r.status_code == 302 and r.headers["Location"].startswith(google.AUTH_URL)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["Location"]).query)
    assert q["client_id"] == ["cid"] and q["access_type"] == ["offline"] and google.SCOPE in q["scope"][0]
    state = q["state"][0]
    bad = client.get("/google/callback?code=x&state=nope", follow_redirects=True).get_data(as_text=True)
    assert "match" in bad and "Try Connect again" in bad
    client.get("/google/connect")            # fresh state
    with client.session_transaction() as s:
        state = s["google_state"]
    ok = client.get(f"/google/callback?code=abc&state={state}", follow_redirects=True).get_data(as_text=True)
    assert "Google Calendar connected" in ok and "Google Calendar is connected" in ok
    conn = db.connect(client.application.config["DB_FILE"])
    assert google.connected(conn)
    assert "Google Calendar is connected" not in client.post("/google/disconnect", follow_redirects=True).get_data(as_text=True)


def test_connect_explains_missing_setup(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    page = client.get("/google/connect", follow_redirects=True).get_data(as_text=True)
    assert "GOOGLE_CLIENT_ID" in page and "One-time Google setup" in page


def test_token_refresh_and_expiry(conn, gapi):
    db.save_settings(conn, {"google": {"refresh_token": "ref", "access_token": "old", "expires_at": time.time() - 10}})
    assert google.access_token(conn) == "acc2"                       # refreshed
    assert google.access_token(conn) == "acc2"                       # cached
    assert sum(1 for c in gapi.calls if c[0] == "POST" and c[1] == google.TOKEN_URL.replace(google.API, "")) == 1
    db.save_settings(conn, {"google": {"refresh_token": "ref", "access_token": "", "expires_at": 0}})
    gapi.refresh_error = "invalid_grant"
    with pytest.raises(google.GoogleError, match="expired"):
        google.access_token(conn)
    assert not google.connected(conn)                                 # cleared, so Settings offers Connect again


# ---------- busy evenings ----------

def test_busy_levels():
    events = [
        {"title": "Dentist", "start": at(MON, 17, 30), "end": at(MON, 18, 15), "all_day": False, "free": False, "declined": False, "ours": False},
        {"title": "Soccer", "start": at(MON + dt.timedelta(1), 17), "end": at(MON + dt.timedelta(1), 19, 30), "all_day": False, "free": False, "declined": False, "ours": False},
        {"title": "Late movie", "start": at(MON + dt.timedelta(2), 21, 30), "end": at(MON + dt.timedelta(2), 23), "all_day": False, "free": False, "declined": False, "ours": False},
        {"title": "Maybe", "start": at(MON + dt.timedelta(3), 18), "end": at(MON + dt.timedelta(3), 19), "all_day": False, "free": True, "declined": False, "ours": False},
        {"title": "Skipped", "start": at(MON + dt.timedelta(4), 18), "end": at(MON + dt.timedelta(4), 19), "all_day": False, "free": False, "declined": True, "ours": False},
        {"title": "Out of office", "start": MON + dt.timedelta(5), "end": MON + dt.timedelta(6), "all_day": True, "free": False, "declined": False, "ours": False},
        {"title": "Cook: Tacos", "start": at(MON + dt.timedelta(6), 18), "end": at(MON + dt.timedelta(6), 19), "all_day": False, "free": False, "declined": False, "ours": True},
    ]
    res = busy.analyse(events, MON, db.DAYS, tz=TZ)
    assert (res["Mon"]["level"], res["Mon"]["minutes"]) == ("busy", 45) and res["Mon"]["label"] == "Dentist 5:30–6:15pm"
    assert res["Tue"]["level"] == "packed" and res["Tue"]["label"] == "Soccer 5–7:30pm"
    assert [res[d]["level"] for d in ("Wed", "Thu", "Fri", "Sun")] == ["free"] * 4     # outside window, free, declined, our own
    assert res["Sat"]["level"] == "packed" and res["Sat"]["label"] == "Out of office"


def test_list_events_reads_google_format(conn, gapi):
    sign_in(conn)
    gapi.items = [ev("Soccer", at(MON, 18), at(MON, 19)),
                  {"summary": "Holiday", "start": {"date": "2026-11-19"}, "end": {"date": "2026-11-20"}},
                  ev("Free time", at(MON, 12), at(MON, 13), transparency="transparent"),
                  ev("Nope", at(MON, 12), at(MON, 13), attendees=[{"self": True, "responseStatus": "declined"}]),
                  ev("Cook: X", at(MON, 12), at(MON, 13), extendedProperties={"private": {google.OURS: "k"}}),
                  ev("Gone", at(MON, 12), at(MON, 13), status="cancelled")]
    got = {e["title"]: e for e in google.list_events(conn, at(MON, 0), at(MON, 23))}
    assert set(got) == {"Soccer", "Holiday", "Free time", "Nope", "Cook: X"}
    assert got["Holiday"]["all_day"] and got["Free time"]["free"] and got["Nope"]["declined"] and got["Cook: X"]["ours"]


# ---------- planner ----------

def _recipe(i, title, minutes, **kw):
    r = {"id": i, "title": title, "cuisine": f"c{i}", "protein": "vegetarian", "prep_minutes": minutes, "servings": 4,
         "tags": [], "archived": 0, "ingredients": [{"name": f"thing{i}", "quantity": 1, "unit": "", "category": "pantry"}]}
    r.update(kw)
    return r


def test_busy_nights_get_quick_dinners_and_packed_nights_pizza():
    recipes = [_recipe(1, "Slow braise", 90), _recipe(2, "Roast", 60), _recipe(3, "Quick pasta", 20),
               _recipe(4, "Margherita Pizza", 25), _recipe(5, "Long stew", 75)]
    hints = {"Tue": {"max_minutes": 25, "theme": "quick", "level": "busy", "label": "x"},
             "Wed": {"max_minutes": 25, "theme": "pizza", "level": "packed", "label": "y"}}
    for seed in range(15):
        picks = {p["day"]: p["recipe_id"] for p in planner.heuristic_pick(
            recipes, {}, {"weekly_budget": 0}, ["Mon", "Tue", "Wed"], planner.upcoming_monday(),
            rng=random.Random(seed), day_hints=hints)}
        assert picks["Tue"] in (3, 4) and picks["Wed"] in (3, 4)      # never the 60+ minute recipes
        assert picks["Wed"] == 4                                      # packed night: the pizza


def test_planner_reads_calendar(conn, gapi):
    sign_in(conn)
    gapi.items = [ev("Soccer", at(MON + dt.timedelta(1), 17), at(MON + dt.timedelta(1), 20))]
    hints, err = busy.hints(conn, MON, ["Mon", "Tue"])
    assert err == "" and list(hints) == ["Tue"] and hints["Tue"]["theme"] == "pizza" and hints["Tue"]["max_minutes"] == 25
    db.save_settings(conn, {"packed_meal": "quick", "quick_minutes": 30})
    busy._cache.clear()
    hints, _ = busy.hints(conn, MON, ["Tue"])
    assert hints["Tue"]["theme"] == "quick" and hints["Tue"]["max_minutes"] == 30
    db.save_settings(conn, {"use_calendar": False})
    assert busy.hints(conn, MON, ["Tue"]) == ({}, "")


def test_calendar_errors_never_break_planning(conn, gapi):
    sign_in(conn)
    db.save_settings(conn, {"google": {"refresh_token": "ref", "access_token": "", "expires_at": 0}})
    gapi.refresh_error = "invalid_grant"
    hints, err = busy.hints(conn, MON, ["Tue"])
    assert hints == {} and "expired" in err
    assert planner.plan_week(conn, MON, use_ai=False, rng=random.Random(1))     # still makes a plan


def test_plan_page_shows_busy_evenings(client, gapi):
    conn = db.connect(client.application.config["DB_FILE"])
    sign_in(conn)
    client.post("/plans", data={"week_start": MON.isoformat()})
    plan = db.latest_plan(conn)
    day = plan["meals"][0]["day"]
    d = MON + dt.timedelta(db.DAYS.index(day))
    gapi.items = [ev("Soccer", at(d, 17), at(d, 20))]
    busy._cache.clear()
    page = client.get(f"/plans/{plan['id']}").get_data(as_text=True)
    assert "Your calendar: 1 busy evening" in page and "Soccer 5–8pm" in page and ">Packed<" in page
    assert "Add to Google Calendar" in page


# ---------- reminders ----------

def _plan_with(conn, rows):
    ids = []
    for i, (title, protein, minutes, day) in enumerate(rows):
        rid = db.add_recipe(conn, {"title": title, "protein": protein, "prep_minutes": minutes, "servings": 2,
                                   "ingredients": [{"name": protein, "quantity": 1, "unit": "lb",
                                                    "category": "meat & seafood" if protein != "vegetarian" else "produce"}],
                                   "steps": ["Cook it."]})
        ids.append((day, rid))
    pid = db.create_plan(conn, MON.isoformat(), [{"day": d, "recipe_id": r, "servings": 2} for d, r in ids], "heuristic", "")
    return db.get_plan(conn, pid)


def test_reminders_build(conn):
    plan = _plan_with(conn, [("Veg curry", "vegetarian", 30, "Mon"), ("Baked salmon", "salmon", 40, "Fri")])
    db.save_settings(conn, {"dinner_time": "19:00"})
    order = {"id": 7, "delivery_date": "2026-11-17"}
    rems = reminders.build(plan, order, db.get_settings(conn), "http://pi:5055")
    by = {r["key"].split(":", 1)[1]: r for r in rems}
    cook = by[f"cook:{plan['meals'][0]['id']}"]
    assert cook["start"] == dt.datetime(2026, 11, 16, 18, 30) and cook["end"] == dt.datetime(2026, 11, 16, 19, 0)
    assert cook["title"] == "Cook: Veg curry" and cook["url"].startswith("http://pi:5055/meals/")
    rate = by[f"rate:{plan['meals'][0]['id']}"]
    assert rate["start"] == dt.datetime(2026, 11, 17, 9, 0) and "Rate Veg curry" in rate["title"]
    assert by["delivery"]["start"] == dt.datetime(2026, 11, 16, 18, 0) and by["delivery"]["title"] == "Groceries arrive tomorrow"
    thaw = [r for r in rems if r["kind"] == "night"]
    assert len(thaw) == 1 and "salmon" in thaw[0]["title"] and thaw[0]["start"] == dt.datetime(2026, 11, 19, 20, 0)   # Thu night, for Fri
    assert rems == sorted(rems, key=lambda r: r["start"])


def test_sync_adds_updates_and_removes(conn, gapi):
    sign_in(conn)
    plan = _plan_with(conn, [("Veg curry", "vegetarian", 30, "Mon"), ("Baked salmon", "salmon", 40, "Fri")])
    rems = reminders.build(plan, None, db.get_settings(conn))
    before = dt.datetime(2026, 11, 1)
    res = reminders.sync_to_google(conn, plan["id"], rems, now=before)
    assert res == {"added": len(rems), "updated": 0, "removed": 0} and len(gapi.events) == len(rems)
    body = next(b for b in gapi.events.values() if b["summary"] == "Cook: Veg curry")
    assert body["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 15}]}
    assert body["extendedProperties"]["private"][google.OURS].startswith(f"{plan['id']}:cook:")
    again = reminders.sync_to_google(conn, plan["id"], rems, now=before)
    assert again == {"added": 0, "updated": len(rems), "removed": 0} and len(gapi.events) == len(rems)    # no duplicates
    gapi.events.pop(next(iter(gapi.events)))                                    # you deleted one in Google Calendar
    healed = reminders.sync_to_google(conn, plan["id"], rems, now=before)
    assert len(gapi.events) == len(rems) and healed["updated"] == len(rems)    # recreated
    fewer = [r for r in rems if r["kind"] != "night"]
    gone = reminders.sync_to_google(conn, plan["id"], fewer, now=before)
    assert gone["removed"] == len(rems) - len(fewer) and len(gapi.events) == len(fewer)
    late = reminders.sync_to_google(conn, plan["id"], rems, now=dt.datetime(2026, 12, 1))     # everything is in the past
    assert late["added"] == 0 and gapi.events == {}


def test_sync_route(client, gapi):
    conn = db.connect(client.application.config["DB_FILE"])
    client.post("/plans", data={"week_start": "2099-01-05"})
    off = client.post("/plans/1/calendar", follow_redirects=True).get_data(as_text=True)
    assert "Connect it under Settings" in off
    sign_in(conn)
    on = client.post("/plans/1/calendar", follow_redirects=True).get_data(as_text=True)
    assert "Google Calendar updated:" in on and len(gapi.events) > 5


def test_delivery_date_creates_a_reminder(client):
    conn = db.connect(client.application.config["DB_FILE"])
    client.post("/plans", data={"week_start": "2099-01-05"})
    oid = int(client.post("/plans/1/order").headers["Location"].rsplit("/", 1)[1])
    client.post(f"/orders/{oid}/placed", data={"total": "", "delivery_date": "2099-01-06"})
    assert db.get_order(conn, oid)["delivery_date"] == "2099-01-06"
    client.post(f"/orders/{oid}/placed", data={"total": "10", "delivery_date": ""})        # blank keeps it
    assert db.get_order(conn, oid)["delivery_date"] == "2099-01-06"
    rems = reminders.build(db.get_plan(conn, 1), db.get_order(conn, oid), db.get_settings(conn))
    assert any(r["title"] == "Groceries arrive tomorrow" for r in rems)


# ---------- phone pushes ----------

@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_post", lambda url, data, headers: sent.append((url, data.decode(), headers)) or 200)
    return sent


def test_ntfy_sends_due_reminders_once(conn, pushes):
    plan = _plan_with(conn, [("Veg curry", "vegetarian", 30, "Mon"), ("Rated one", "vegetarian", 30, "Tue")])
    assert notify.run_once(conn, dt.datetime(2026, 11, 16, 18, 10)) == 0                   # no topic yet
    db.save_settings(conn, {"ntfy_topic": "my-topic", "app_url": "http://pi:5055"})
    now = dt.datetime(2026, 11, 16, 18, 10)                                                 # cook starts 18:00
    assert notify.run_once(conn, now) == 1
    url, body, headers = pushes[0]
    assert url == "https://ntfy.sh/my-topic" and "Ready by" in body and headers["Title"] == "Cook: Veg curry"
    assert headers["Click"].startswith("http://pi:5055/meals/")
    assert notify.run_once(conn, now + dt.timedelta(minutes=5)) == 0                        # not twice
    # next morning: rate Mon's dinner, but Tue's was already rated
    db.save_feedback(conn, plan["meals"][1]["id"], plan["meals"][1]["recipe_id"], 5, "yes", "")
    titles = [r["title"] for r in notify.due(conn, dt.datetime(2026, 11, 17, 9, 5))]
    assert any("Rate Veg curry" in t for t in titles)
    titles = [r["title"] for r in notify.due(conn, dt.datetime(2026, 11, 18, 9, 5))]
    assert not any("Rate Rated one" in t for t in titles)                                    # already rated: no nag
    assert notify.run_once(conn, dt.datetime(2026, 11, 20, 12, 0)) == 0                     # stale reminders are dropped


def test_ntfy_test_button(client, pushes):
    assert "Choose a topic" in client.post("/notify/test", data={"ntfy_topic": ""}, follow_redirects=True).get_data(as_text=True)
    page = client.post("/notify/test", data={"ntfy_topic": "abc", "ntfy_server": "https://ntfy.example"}, follow_redirects=True).get_data(as_text=True)
    assert "Sent a test notification" in page and pushes[0][0] == "https://ntfy.example/abc"


def test_settings_saves_calendar_options(client):
    form = {"household_size": "2", "max_prep_minutes": "60", "new_recipe_pct": "0", "day-Mon": "on",
            "busy_start": "16", "busy_end": "20", "quick_minutes": "30", "packed_meal": "quick",
            "dinner_time": "7:15", "ntfy_topic": "my topic!/x", "app_url": "http://pi:5055/"}
    client.post("/settings", data=form)
    s = db.get_settings(db.connect(client.application.config["DB_FILE"]))
    assert (s["busy_start"], s["busy_end"], s["quick_minutes"], s["packed_meal"]) == (16, 20, 30, "quick")
    assert s["dinner_time"] == "07:15" and s["ntfy_topic"] == "mytopicx" and s["app_url"] == "http://pi:5055"
    assert s["use_calendar"] is False                          # the box was unchecked in this form
    client.post("/settings", data=dict(form, dinner_time="nonsense"))
    assert db.get_settings(db.connect(client.application.config["DB_FILE"]))["dinner_time"] == "18:30"
