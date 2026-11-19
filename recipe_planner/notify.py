"""Phone push reminders through ntfy (https://ntfy.sh): install the free ntfy app, subscribe to a private topic."""
from __future__ import annotations

import datetime as dt
import threading
import time
import urllib.request

from . import db, reminders

GRACE = dt.timedelta(minutes=45)       # a reminder this late is still worth sending (app was asleep)


def _post(url: str, data: bytes, headers: dict) -> int:
    """One HTTP POST; tests replace this."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def send(settings: dict, title: str, message: str, click: str = "") -> None:
    topic = (settings.get("ntfy_topic") or "").strip().strip("/")
    if not topic:
        raise ValueError("Choose a topic name first.")
    server = (settings.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
    headers = {"Title": title.encode("ascii", "ignore").decode() or "Meal Planner"}
    if click:
        headers["Click"] = click
    _post(f"{server}/{topic}", message.encode("utf-8"), headers)


def due(conn, now: dt.datetime, app_url: str = "") -> list[dict]:
    """Reminders whose time has come and that haven't been sent. 'Rate' ones are skipped once rated."""
    settings = db.get_settings(conn)
    plan = db.latest_plan(conn)
    if not plan:
        return []
    sent = {r["key"] for r in conn.execute("SELECT key FROM sent_reminders")}
    rated = {m["id"] for m in plan["meals"] if m.get("fb_rating") is not None}
    out = []
    for r in reminders.build(plan, db.order_for_plan(conn, plan["id"]), settings, app_url):
        if r["key"] in sent or not (now - GRACE <= r["start"] <= now):
            continue
        if r["kind"] == "rate" and r.get("meal_id") in rated:
            continue
        out.append(r)
    return out


def run_once(conn, now: dt.datetime | None = None) -> int:
    """Send whatever is due. Returns how many went out."""
    settings = db.get_settings(conn)
    if not (settings.get("ntfy_topic") or "").strip():
        return 0
    sent = 0
    for r in due(conn, now or dt.datetime.now()):
        try:
            send(settings, r["title"], r["body"] or r["title"], r["url"])
        except Exception:                                # offline, ntfy down: try again next tick
            continue
        conn.execute("INSERT OR IGNORE INTO sent_reminders(key) VALUES (?)", (r["key"],))
        conn.commit()
        sent += 1
    return sent


def start_scheduler(db_file: str, every: int = 300) -> threading.Thread:
    """Check for due reminders every few minutes while the app runs."""
    def loop():
        while True:
            try:
                conn = db.connect(db_file)
                try:
                    run_once(conn)
                finally:
                    conn.close()
            except Exception:
                pass
            time.sleep(every)

    t = threading.Thread(target=loop, daemon=True, name="reminders")
    t.start()
    return t
