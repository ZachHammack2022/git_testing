"""Which evenings are busy, from your calendar, so those nights get a quick dinner.

Busy = timed events overlapping the dinner window (default 5-9pm). A few minutes up to 90 is "busy"
(a quick recipe); 90 or more is "packed" (an oven pizza or other easy night, your choice in Settings).
"""
from __future__ import annotations

import datetime as dt
import re
import time

from . import db, google

AWAY = re.compile(r"\b(out of office|ooo|travel|trip|flight|vacation|holiday|away|off)\b", re.I)
PACKED_MINUTES = 90
_cache: dict[tuple, tuple[float, list]] = {}
CACHE_SECONDS = 300


def local_tz() -> dt.tzinfo:
    return dt.datetime.now().astimezone().tzinfo


def _fmt(t: dt.datetime) -> str:
    h = t.hour % 12 or 12
    return f"{h}{'' if t.minute == 0 else f':{t.minute:02d}'}"


def _range(s: dt.datetime, e: dt.datetime) -> str:
    suffix = "pm" if e.hour >= 12 else "am"
    return f"{_fmt(s)}-{_fmt(e)}{suffix}".replace("-", "–")


def analyse(events: list[dict], week_start: dt.date, days: list[str], start_hour: int = 17,
            end_hour: int = 21, tz: dt.tzinfo | None = None) -> dict[str, dict]:
    """{day: {level: free|busy|packed, minutes, label}} for the given weekday names."""
    tz = tz or local_tz()
    out = {}
    for name in days:
        day = week_start + dt.timedelta(days=db.DAYS.index(name))
        win_s = dt.datetime.combine(day, dt.time(start_hour), tz)
        win_e = dt.datetime.combine(day, dt.time(end_hour), tz)
        minutes, label, first = 0.0, "", None
        for ev in events:
            if ev["free"] or ev["declined"] or ev["ours"]:
                continue
            if ev["all_day"]:
                if ev["start"] <= day < ev["end"] and AWAY.search(ev["title"]):
                    minutes, label = max(minutes, (end_hour - start_hour) * 60), ev["title"]
                continue
            s, e = ev["start"].astimezone(tz), ev["end"].astimezone(tz)
            overlap = (min(e, win_e) - max(s, win_s)).total_seconds() / 60
            if overlap > 0:
                minutes += overlap
                if first is None or s < first[0]:
                    first = (s, f"{ev['title']} {_range(s, e)}")
        if first and not label:
            label = first[1]
        level = "free" if minutes <= 0 else ("packed" if minutes >= PACKED_MINUTES else "busy")
        out[name] = {"level": level, "minutes": round(minutes), "label": label}
    return out


def enabled(conn) -> bool:
    return google.connected(conn) and bool(db.get_settings(conn).get("use_calendar", True))


def week_status(conn, week_start: dt.date, days: list[str]) -> tuple[dict[str, dict], str]:
    """(busy info by day, error message). Reads the calendar at most every 5 minutes per week."""
    if not days or not enabled(conn):
        return {}, ""
    s = db.get_settings(conn)
    tz = local_tz()
    key = (week_start.isoformat(), s.get("google", {}).get("refresh_token", "")[:8])
    hit = _cache.get(key)
    try:
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            events = hit[1]
        else:
            lo = dt.datetime.combine(week_start, dt.time(0), tz)
            events = google.list_events(conn, lo, lo + dt.timedelta(days=8))
            _cache[key] = (time.time(), events)
    except (google.GoogleError, LookupError) as e:
        return {}, str(e)
    return analyse(events, week_start, days, int(s.get("busy_start") or 17), int(s.get("busy_end") or 21), tz), ""


def hints(conn, week_start: dt.date, days: list[str]) -> tuple[dict[str, dict], str]:
    """What the planner should do on each busy day: {day: {max_minutes, theme, label, level}}."""
    status, err = week_status(conn, week_start, days)
    s = db.get_settings(conn)
    quick = int(s.get("quick_minutes") or 25)
    packed_theme = "pizza" if s.get("packed_meal", "pizza") == "pizza" else "quick"
    out = {}
    for day, info in status.items():
        if info["level"] == "busy":
            out[day] = {"max_minutes": quick, "theme": "quick", **info}
        elif info["level"] == "packed":
            out[day] = {"max_minutes": quick, "theme": packed_theme, **info}
    return out, err
