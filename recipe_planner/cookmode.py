"""Helpers for cook mode: find "simmer 10 minutes"-style durations in a recipe step."""
from __future__ import annotations

import re

_NUM = r"\d+(?:\s\d+/\d+|\.\d+|/\d+)?"
_UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)"
_RE = re.compile(rf"(?<![\w/.])({_NUM})(?:\s*(?:-|–|to)\s*({_NUM}))?\s*{_UNIT}\b", re.I)
_SECONDS = {"s": 1, "m": 60, "h": 3600}


def _to_float(text: str) -> float:
    total = 0.0
    for part in text.split():
        if "/" in part:
            a, b = part.split("/", 1)
            total += float(a) / float(b) if float(b) else 0.0
        else:
            total += float(part)
    return total


def find_timers(step: str) -> list[dict]:
    """Durations mentioned in a step, e.g. [{"seconds": 600, "label": "10 min"}].

    A range ("10-12 minutes") uses its upper end so food isn't undercooked. Repeats are dropped."""
    found, seen = [], set()
    for m in _RE.finditer(step or ""):
        amount = _to_float(m.group(2) or m.group(1))
        unit = _SECONDS[m.group(3)[0].lower()]
        seconds = int(round(amount * unit))
        if seconds <= 0 or seconds in seen:
            continue
        seen.add(seconds)
        found.append({"seconds": seconds, "label": _label(seconds)})
    return found


def _label(seconds: int) -> str:
    if seconds >= 3600 and seconds % 60 == 0:
        h, m = divmod(seconds // 60, 60)
        return f"{h} hr {m} min" if m else f"{h} hr"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} min"
    if seconds >= 60:
        return f"{seconds // 60} min {seconds % 60} sec"
    return f"{seconds} sec"
