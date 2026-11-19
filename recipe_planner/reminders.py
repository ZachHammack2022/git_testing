"""The reminders for a week: when to start cooking, thaw the protein, groceries arriving, rate the dinner.

One list feeds both places they can go: Google Calendar events (with popup alerts) and ntfy phone pushes.
"""
from __future__ import annotations

import datetime as dt

from . import db, google, prep

NIGHT_TIME = dt.time(20, 0)        # the evening-before reminders (thaw, marinate, groceries)
RATE_TIME = dt.time(9, 0)          # "how was dinner?" the next morning


def _dinner_time(settings: dict) -> dt.time:
    try:
        h, m = str(settings.get("dinner_time") or "18:30").split(":")
        return dt.time(int(h), int(m))
    except (ValueError, TypeError):
        return dt.time(18, 30)


def _base(settings: dict, app_url: str) -> str:
    return (settings.get("app_url") or app_url or "").rstrip("/")


def build(plan: dict, order: dict | None, settings: dict, app_url: str = "") -> list[dict]:
    """Reminders for a plan, soonest first: [{key, kind, start, end, title, body, url, alert}] (naive local times)."""
    week = dt.date.fromisoformat(plan["week_start"])
    base = _base(settings, app_url)
    dinner = _dinner_time(settings)
    out = []

    def day_of(meal):
        return week + dt.timedelta(days=db.DAYS.index(meal["day"]))

    for m in plan["meals"]:
        if m.get("day") not in db.DAYS:
            continue
        r, day = m["recipe"], day_of(m)
        ready = dt.datetime.combine(day, dinner)
        cook_min = max(int(r.get("prep_minutes") or 30), 15)
        out.append({"key": f"{plan['id']}:cook:{m['id']}", "kind": "cook", "start": ready - dt.timedelta(minutes=cook_min),
                    "end": ready, "title": f"Cook: {r['title']}", "alert": 15,
                    "body": f"{cook_min} min, serves {m['servings']}. Ready by {ready:%-I:%M%p}.".lower().replace("ready by", "Ready by"),
                    "url": f"{base}/meals/{m['id']}/cook" if base else ""})
        nxt = dt.datetime.combine(day + dt.timedelta(days=1), RATE_TIME)
        out.append({"key": f"{plan['id']}:rate:{m['id']}", "kind": "rate", "start": nxt, "end": nxt + dt.timedelta(minutes=15),
                    "title": f"How was dinner? Rate {r['title']}", "alert": 0, "body": "Ratings shape next week's plan.",
                    "url": f"{base}/plans/{plan['id']}/feedback" if base else "", "meal_id": m["id"]})

    for t in prep.builtin_plan(plan).get("night_before", []):
        name = t["day"]
        d = week - dt.timedelta(days=1) if name.startswith("Sun (weekend)") else week + dt.timedelta(days=db.DAYS.index(name))
        start = dt.datetime.combine(d, NIGHT_TIME)
        out.append({"key": f"{plan['id']}:night:{t['id']}", "kind": "night", "start": start, "end": start + dt.timedelta(minutes=15),
                    "title": t["text"], "alert": 0, "body": "", "url": f"{base}/plans/{plan['id']}/prep" if base else ""})

    if order and order.get("delivery_date"):
        try:
            arrive = dt.date.fromisoformat(order["delivery_date"])
            start = dt.datetime.combine(arrive - dt.timedelta(days=1), dt.time(18, 0))
            out.append({"key": f"{plan['id']}:delivery", "kind": "delivery", "start": start, "end": start + dt.timedelta(minutes=15),
                        "title": "Groceries arrive tomorrow", "alert": 0, "body": "Make room in the fridge.",
                        "url": f"{base}/orders/{order['id']}" if base else ""})
        except ValueError:
            pass
    return sorted(out, key=lambda x: x["start"])


# ---------- Google Calendar ----------

def _event_body(rem: dict) -> dict:
    tz = dt.datetime.now().astimezone().tzinfo
    desc = "\n".join(x for x in (rem["body"], rem["url"]) if x)
    return {
        "summary": rem["title"], "description": desc,
        "start": {"dateTime": rem["start"].replace(tzinfo=tz).isoformat()},
        "end": {"dateTime": rem["end"].replace(tzinfo=tz).isoformat()},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": rem["alert"]}]},
        "extendedProperties": {"private": {google.OURS: rem["key"]}},
    }


def sync_to_google(conn, plan_id: int, reminders: list[dict], now: dt.datetime | None = None) -> dict:
    """Make Google Calendar match these reminders: add new ones, update changed ones, remove stale ones.
    Only future reminders are sent; events this app made earlier for the same plan are cleaned up."""
    now = now or dt.datetime.now()
    wanted = {r["key"]: r for r in reminders if r["end"] > now}
    have = {row["key"]: row["event_id"] for row in
            conn.execute("SELECT key, event_id FROM calendar_events WHERE plan_id=?", (plan_id,))}
    added = updated = removed = 0
    for key, rem in wanted.items():
        event_id = google.save_event(conn, have.get(key), _event_body(rem))
        added, updated = (added + 1, updated) if key not in have else (added, updated + 1)
        conn.execute("INSERT OR REPLACE INTO calendar_events(key, event_id, plan_id) VALUES (?,?,?)", (key, event_id, plan_id))
    for key, event_id in have.items():
        if key not in wanted:
            google.delete_event(conn, event_id)
            conn.execute("DELETE FROM calendar_events WHERE key=?", (key,))
            removed += 1
    conn.commit()
    return {"added": added, "updated": updated, "removed": removed}
