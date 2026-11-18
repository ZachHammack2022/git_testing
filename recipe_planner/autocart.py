"""Fill your Amazon Whole Foods cart from an order, in a real browser window.

Start it from the order page ("Fill my cart"), or from a terminal:

    pip install playwright && python -m playwright install chromium
    python -m recipe_planner.autocart            # latest unplaced order
    python -m recipe_planner.autocart --order 3

A visible browser opens with its own profile (kept in instance/browser-profile), so you sign in
to Amazon once and stay signed in. For each unticked item it opens the item's link: your saved
product, or a Whole Foods search where it takes the first non-sponsored result. Added items are
ticked in the app, and the product it chose is remembered for next time (change it on the order
page if it guessed wrong). Sign-in pages and "type the characters" checks pause it until you've
dealt with them in the window. It never clicks through Amazon's checks itself.

It never checks out. It adds one of each item, so review quantities in the cart.
Amazon changes its pages often, so this is a helper, not a guarantee.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

from . import db, ordering

# "Add", "Add to cart", "Add Organic Avocado to cart" — but not "Add to List" / "Add to Registry".
ADD_BUTTON = re.compile(r"^\s*add(\s+to\s+cart)?\s*$|^\s*add\b.*\bto\s+(your\s+)?cart\s*$", re.I)
GREETINGS = ["#nav-link-accountList-nav-line-1", "#nav-link-accountList .nav-line-1",
             "#glow-ingress-line1", "#nav-greeting-name"]
FAIL_STREAK_PAUSE = 3
ACTIVE_STATES = ("starting", "signin", "waiting", "running")
AMAZON_HOME = os.environ.get("AUTOCART_HOME", "https://www.amazon.com/")
CART_URL = os.environ.get("AUTOCART_CART", ordering.AMAZON_CART)
RESULT_CARDS = '[data-component-type="s-search-result"]'


# ---------------- status shared with the web app ----------------

def status_path(order_id: int) -> Path:
    return Path(db.db_path()).parent / f"autofill-{order_id}.json"


def write_status(order_id: int, **fields) -> None:
    path = status_path(order_id)
    current = read_status(order_id) or {}
    current.update(fields, updated=time.time())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current))
    tmp.replace(path)


def read_status(order_id: int) -> dict | None:
    try:
        return json.loads(status_path(order_id).read_text())
    except (OSError, ValueError):
        return None


def is_running(order_id: int) -> bool:
    st = read_status(order_id)
    if not st or st.get("state") not in ACTIVE_STATES:
        return False
    pid = st.get("pid")
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def debug_dir(order_id: int) -> Path:
    return Path(db.db_path()).parent / f"autofill-{order_id}-debug"


def _snapshot(page, order_id: int, key: str) -> str | None:
    """Save what the browser saw for an item it couldn't add. Returns the file name."""
    d = debug_dir(order_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:60] + ".png"
        page.screenshot(path=str(d / name))
        return name
    except Exception:
        return None


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------- page helpers ----------------

def pick_order(conn, order_id: int | None) -> dict | None:
    if order_id:
        return db.get_order(conn, order_id)
    row = conn.execute(
        "SELECT id FROM orders WHERE status != 'placed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return db.get_order(conn, row["id"]) if row else None


def _visible(loc) -> bool:
    try:
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def _add_buttons(scope):
    return [
        scope.locator("#freshAddToCartButton input, #freshAddToCartButton button"),
        scope.locator("#add-to-cart-button"),
        scope.get_by_role("button", name=ADD_BUTTON),
        scope.locator("input[name='submit.addToCart'], button[name='submit.addToCart']"),
    ]


def _try_add(page, scope=None) -> bool:
    """Click the first visible add-to-cart control in scope (default: whole page)."""
    for loc in _add_buttons(scope or page):
        try:
            if _visible(loc):
                loc.first.click(timeout=5000)
                page.wait_for_timeout(1500)
                return True
        except Exception:  # detached, covered, navigated away...
            continue
    return False


def _is_interstitial(url: str) -> bool:
    return "/checkpoint/" in url or "validateCaptcha" in url or "/errors/" in url


def needs_human(page) -> bool:
    """Sign-in page or bot check that only the person can get past."""
    url = page.url or ""
    if "/ap/signin" in url or _is_interstitial(url):
        return True
    return _visible(page.locator("#captchacharacters, form[action*='validateCaptcha']"))


def settle(page, say) -> bool:
    """After a navigation: if Amazon shows a sign-in page or any bot check, hand over to the
    person and wait. We never click through Amazon's checks ourselves. False = gave up."""
    return not needs_human(page) or wait_for_human(page, say)


def is_signed_in(page) -> bool | None:
    """True = Amazon greets you by name, False = clearly signed out, None = can't tell."""
    if "/ap/signin" in (page.url or ""):
        return False
    loc = page.locator("#nav-link-accountList-nav-line-1, #nav-link-accountList .nav-line-1")
    if _visible(loc):
        text = loc.first.inner_text().strip().lower()
        if "sign in" in text:
            return False
        if text.startswith("hello"):
            return True
    try:
        header = page.locator("header, #navbar, #nav-belt").first.inner_text(timeout=2000).lower()
    except Exception:
        return None
    return False if "hello, sign in" in header else None


def wait_for_human(page, say, timeout: float = 600) -> bool:
    say("waiting", "Over to you: sign in or finish the check in the browser window. "
        "I'll continue automatically.")
    end = time.time() + timeout
    while time.time() < end:
        page.wait_for_timeout(2000)
        if not needs_human(page):
            return True
    return False


def _first_real_result(page):
    """First search result that isn't sponsored, or None."""
    cards = page.locator(RESULT_CARDS)
    for i in range(min(cards.count(), 10)):
        card = cards.nth(i)
        try:
            text = card.inner_text(timeout=2000)
        except Exception:
            continue
        if re.search(r"\bSponsored\b", text):
            continue
        return card
    return None


def add_item(page, item: dict, say) -> dict:
    """Open the item's link and add it. Returns {ok, asin, title}."""
    page.goto(item["url"], wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1200)
    if not settle(page, say):
        return {"ok": False}
    if item.get("has_pick"):
        return {"ok": _try_add(page)}

    card = _first_real_result(page)
    if card is None:
        return {"ok": _try_add(page)}  # maybe the search landed on a product page
    asin = card.get_attribute("data-asin") or ""
    title_loc = card.locator("h2")
    title = title_loc.first.inner_text().strip() if title_loc.count() else ""
    if _try_add(page, card):
        return {"ok": True, "asin": asin, "title": title}
    link = card.locator("h2 a, a[href*='/dp/']")
    if link.count():
        link.first.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1200)
        if not settle(page, say):
            return {"ok": False}
        return {"ok": _try_add(page), "asin": asin, "title": title}
    return {"ok": False}


# ---------------- main loop ----------------

def _launch(pw, profile: str, headless: bool):
    kwargs = {"headless": headless, "viewport": {"width": 1280, "height": 900}}
    exe = os.environ.get("AUTOCART_BROWSER")
    if exe:
        return pw.chromium.launch_persistent_context(profile, executable_path=exe, **kwargs)
    try:  # a real Chrome install looks less like a bot than bundled Chromium
        return pw.chromium.launch_persistent_context(profile, channel="chrome", **kwargs)
    except Exception:
        return pw.chromium.launch_persistent_context(profile, **kwargs)


def fill_cart(order_id: int | None = None, interactive: bool = True, learn: bool = True,
              limit: int | None = None, headless: bool = False, profile_dir: str | None = None,
              stay_open: bool = True) -> int:
    from playwright.sync_api import sync_playwright

    conn = db.connect()
    db.init(conn)
    order = pick_order(conn, order_id)
    if not order:
        print("No open order found. Start one from the shopping list first.")
        return 1
    oid = order["id"]
    todo = [i["key"] for i in order["items"] if not i.get("added")]
    if limit:
        todo = todo[:limit]
    write_status(oid, state="starting", pid=os.getpid(), added=0, total=len(todo), skipped=[],
                 message="Opening the browser…")

    def say(state, message, **extra):
        print(message)
        write_status(oid, state=state, message=message, **extra)

    if not todo:
        say("done", "Everything on this order is already ticked off.")
        return 0

    profile = profile_dir or str(Path(db.db_path()).parent / "browser-profile")
    added, skipped = 0, []
    try:
        with sync_playwright() as pw:
            ctx = _launch(pw, profile, headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(AMAZON_HOME, wait_until="domcontentloaded")
            settle(page, say)
            if interactive:
                input("Sign in to Amazon in the browser window if needed (and set your Whole Foods "
                      "delivery address), then press Enter here to start... ")
            elif is_signed_in(page) is not True:
                say("signin", "Sign in to Amazon in the browser window that just opened (and make "
                    "sure your Whole Foods delivery address is set). I'll start as soon as you're in.")
                end = time.time() + 900
                while time.time() < end:
                    page.wait_for_timeout(2000)
                    if (read_status(oid) or {}).get("go"):
                        break  # you pressed "I'm signed in — start"
                    if "/ap/" in (page.url or ""):
                        continue  # mid sign-in: don't navigate away
                    if is_signed_in(page) is True:
                        break
                else:
                    say("error", "Timed out waiting for sign-in. Press Fill my cart to try again.")
                    ctx.close()
                    return 1
                page.goto(AMAZON_HOME, wait_until="domcontentloaded")

            fail_streak = 0
            for n, key in enumerate(todo, 1):
                order = db.get_order(conn, oid)  # re-read: you may be ticking things meanwhile
                item = next((i for i in order["items"] if i["key"] == key), None)
                if not item or item.get("added"):
                    continue
                say("running", f"Adding {item['name']} ({n} of {len(todo)})…",
                    current=item["name"], added=added, skipped=skipped)
                try:
                    result = add_item(page, item, say)
                except Exception as e:
                    print(f"  {item['name']}: {e.__class__.__name__}")
                    result = {"ok": False}
                order = db.get_order(conn, oid)
                if result["ok"]:
                    added += 1
                    learned = None
                    if learn and result.get("asin") and not item.get("has_pick"):
                        label = "auto-picked: " + (result.get("title") or "")[:60]
                        url = ordering.whole_foods_product_url(result["asin"])
                        db.set_product_pick(conn, key, url, label)
                        learned = {"url": url, "has_pick": True, "pick_label": label}
                    for i in order["items"]:
                        if i["key"] == key:
                            i["added"] = True
                            i.update(learned or {})
                    db.save_order_items(conn, oid, order["items"])
                    fail_streak = 0
                else:
                    skipped.append(item["name"])
                    shot = _snapshot(page, oid, key)
                    if shot:
                        shots = (read_status(oid) or {}).get("screenshots", {})
                        shots[item["name"]] = shot
                        write_status(oid, screenshots=shots)
                    fail_streak += 1
                write_status(oid, added=added, skipped=skipped)
                if fail_streak >= FAIL_STREAK_PAUSE and added == 0:
                    msg = (f"The first {fail_streak} items couldn't be added. Usually that means "
                           "you're not signed in, or Whole Foods delivery isn't set up for your "
                           "address. Check the browser window, then press Fill my cart again. "
                           "Screenshots of what I saw are on the order page.")
                    say("error", msg)
                    if stay_open and not interactive:
                        try:
                            page.wait_for_event("close", timeout=900_000)
                        except Exception:
                            pass
                    ctx.close()
                    return 1
                page.wait_for_timeout(int(random.uniform(1500, 3500)))  # be gentle

            page.goto(CART_URL, wait_until="domcontentloaded")
            msg = f"Done: added {added} of {len(todo)}. Review your cart in the browser window."
            if skipped:
                msg += " Add these by hand: " + ", ".join(skipped) + "."
            say("done", msg, current="", added=added, skipped=skipped)
            if interactive:
                input("Press Enter to close the browser... ")
            elif stay_open:
                try:  # leave the cart up until you close the window (max 1 hour)
                    page.wait_for_event("close", timeout=3_600_000)
                except Exception:
                    pass
            ctx.close()
    except Exception as e:
        say("error", f"Stopped: {e.__class__.__name__}: {str(e)[:200]}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--order", type=int, help="order id (default: latest unplaced order)")
    p.add_argument("--profile", help="browser profile directory")
    p.add_argument("--limit", type=int, help="only try the first N items")
    p.add_argument("--no-prompt", action="store_true", help="don't wait for Enter (used by the web app)")
    p.add_argument("--no-learn", action="store_true", help="don't remember the products it picks")
    p.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    if not playwright_available():
        print("Needs Playwright:  pip install playwright && python -m playwright install chromium")
        return 2
    return fill_cart(a.order, interactive=not a.no_prompt, learn=not a.no_learn, limit=a.limit,
                     headless=a.headless, profile_dir=a.profile, stay_open=not a.headless)


if __name__ == "__main__":
    sys.exit(main())
