"""Google sign-in plus the few Google Calendar calls the app needs: read events, add and remove ours.

Setup (once): create a Google Cloud project, enable the Google Calendar API, create an OAuth client of
type "Web application" with the redirect URI http://127.0.0.1:5055/google/callback, then start the app
with GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET set. The client id and secret are never stored in the
database; only your sign-in (a refresh token) is.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3/calendars/primary"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
OURS = "mealplanner"          # extended property that marks events this app created


class GoogleError(Exception):
    """Something the user can act on: not signed in, sign-in expired, Google unreachable."""


def client() -> tuple[str, str] | None:
    cid, secret = os.environ.get("GOOGLE_CLIENT_ID", "").strip(), os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    return (cid, secret) if cid and secret else None


def configured() -> bool:
    return client() is not None


def _request(method: str, url: str, *, form: dict | None = None, body: dict | None = None,
             token: str | None = None, timeout: int = 20) -> tuple[int, dict]:
    """One HTTP call; returns (status, parsed JSON). Tests replace this function."""
    data, headers = None, {}
    if form is not None:
        data, headers["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        raise GoogleError(f"Couldn't reach Google: {e}") from e


# ---------- sign-in ----------

def auth_url(redirect_uri: str, state: str) -> str:
    cid, _ = client() or ("", "")
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state})


def _token_call(form: dict) -> dict:
    cid, secret = client() or ("", "")
    status, data = _request("POST", TOKEN_URL, form=dict(form, client_id=cid, client_secret=secret))
    if status != 200:
        raise GoogleError(data.get("error_description") or data.get("error") or f"Google said {status}")
    return data


def _stash(conn, tokens: dict, keep_refresh: str = "") -> None:
    from . import db
    db.save_settings(conn, {"google": {
        "refresh_token": tokens.get("refresh_token") or keep_refresh,
        "access_token": tokens.get("access_token", ""),
        "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
    }})


def finish_sign_in(conn, code: str, redirect_uri: str) -> None:
    tokens = _token_call({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
    if not tokens.get("refresh_token"):
        raise GoogleError("Google didn't return a long-lived sign-in. Remove the app under your Google "
                          "account's security settings and connect again.")
    _stash(conn, tokens)


def connected(conn) -> bool:
    from . import db
    return bool((db.get_settings(conn).get("google") or {}).get("refresh_token"))


def disconnect(conn) -> None:
    from . import db
    db.save_settings(conn, {"google": {}})


def access_token(conn) -> str:
    from . import db
    g = db.get_settings(conn).get("google") or {}
    if not g.get("refresh_token"):
        raise GoogleError("Google Calendar isn't connected. Connect it under Settings.")
    if g.get("access_token") and g.get("expires_at", 0) > time.time() + 60:
        return g["access_token"]
    try:
        tokens = _token_call({"grant_type": "refresh_token", "refresh_token": g["refresh_token"]})
    except GoogleError as e:
        if "invalid_grant" in str(e) or "expired" in str(e).lower() or "revoked" in str(e).lower():
            disconnect(conn)
            raise GoogleError("Your Google sign-in expired. Connect again under Settings.") from e
        raise
    _stash(conn, tokens, keep_refresh=g["refresh_token"])
    return tokens["access_token"]


def api(conn, method: str, path: str = "/events", params: dict | None = None, body: dict | None = None) -> dict:
    url = API + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    status, data = _request(method, url, body=body, token=access_token(conn))
    if status in (200, 201, 204):
        return data
    if status in (404, 410):
        raise LookupError(path)
    msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
    raise GoogleError(f"Google Calendar said: {msg or status}")


# ---------- events ----------

def _when(part: dict) -> tuple[dt.datetime | dt.date | None, bool]:
    if part.get("dateTime"):
        return dt.datetime.fromisoformat(part["dateTime"].replace("Z", "+00:00")), False
    if part.get("date"):
        return dt.date.fromisoformat(part["date"]), True
    return None, False


def list_events(conn, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Events between two aware datetimes: [{title, start, end, all_day, free, declined, ours}]."""
    out, token = [], None
    for _ in range(4):                                          # up to ~1000 events
        params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                  "orderBy": "startTime", "maxResults": 250}
        if token:
            params["pageToken"] = token
        data = api(conn, "GET", "/events", params)
        for it in data.get("items", []):
            if it.get("status") == "cancelled":
                continue
            s, all_day = _when(it.get("start", {}))
            e, _ = _when(it.get("end", {}))
            if s is None:
                continue
            out.append({
                "title": it.get("summary") or "(busy)", "start": s, "end": e or s, "all_day": all_day,
                "free": it.get("transparency") == "transparent",
                "declined": any(a.get("self") and a.get("responseStatus") == "declined" for a in it.get("attendees", [])),
                "ours": OURS in (it.get("extendedProperties", {}).get("private", {})),
            })
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def save_event(conn, event_id: str | None, body: dict) -> str:
    """Create an event, or update it (recreating it if it was deleted in Google Calendar). Returns its id."""
    if event_id:
        try:
            return api(conn, "PATCH", f"/events/{event_id}", body=body)["id"]
        except LookupError:
            pass
    return api(conn, "POST", "/events", body=body)["id"]


def delete_event(conn, event_id: str) -> None:
    try:
        api(conn, "DELETE", f"/events/{event_id}")
    except LookupError:
        pass                                                    # already gone
