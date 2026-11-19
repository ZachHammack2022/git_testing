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
ACTIVE_STATES = ("starting", "signin", "human", "waiting", "running", "syncing", "previewing")
BACKGROUND_STATES = ("syncing", "previewing")  # background jobs, not a cart-filling run
PAUSE_BETWEEN_ITEMS = (2.5, 5.0)  # seconds; a human-ish pace makes Amazon's checks rarer
AMAZON_HOME = os.environ.get("AUTOCART_HOME", "https://www.amazon.com/")
CART_URL = os.environ.get("AUTOCART_CART", ordering.AMAZON_CART)
# Whole Foods items live in their own cart; the main cart page may only link to it.
WF_CART_URL = os.environ.get(
    "AUTOCART_WF_CART",
    f"https://www.amazon.com/cart/localmarket?almBrandId={ordering.WHOLE_FOODS_BRAND}")
CART_ITEMS = ("#sc-active-cart [data-asin], .sc-list-item[data-asin], "
              "[data-name='Active Items'] [data-asin], .sc-list-item-content[data-asin]")
SYNC_STATES = ("syncing",)
RESULT_CARDS = '[data-component-type="s-search-result"]'
SEARCH_BASE = os.environ.get("AUTOCART_SEARCH")  # tests point this at a fake Amazon
PREVIEW_OPTIONS = 6


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
    if hasattr(os, "WNOHANG"):
        # Jobs are children of the web app: an exited one lingers as a zombie (which still
        # "exists") until reaped. Reap it here so it reads as finished.
        try:
            done_pid, _ = os.waitpid(pid, os.WNOHANG)
            if done_pid == pid:
                return False
        except ChildProcessError:
            pass  # not our child (e.g. started from a terminal)
        except OSError:
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
    """Hand over to the person for a sign-in page or an Amazon "are you human" check.

    We deliberately never click through these checks ourselves: they exist to stop
    automation, and getting around them could get the Amazon account flagged. Instead the
    window comes to the front, the order page alerts you, and we continue the moment it clears.
    """
    signin = "/ap/signin" in (page.url or "")
    try:
        page.bring_to_front()
    except Exception:
        pass
    say("human", "Amazon wants you to sign in again." if signin else
        "Amazon is asking you to confirm you're human. Click the button in the browser window "
        "and I'll pick up where I left off.", needs="signin" if signin else "check")
    end = time.time() + timeout
    while time.time() < end:
        page.wait_for_timeout(1500)
        if not needs_human(page):
            say("running", "Thanks! Continuing…", needs="")
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
                    asin = result.get("asin") or asin_from_url(item.get("url", ""))
                    for i in order["items"]:
                        if i["key"] == key:
                            i["added"] = True
                            i.update(learned or {})
                            if asin:
                                i["asin"] = asin
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
                page.wait_for_timeout(int(random.uniform(*PAUSE_BETWEEN_ITEMS) * 1000))

            # Check what really made it into the cart, so the ticks match Amazon.
            say("running", "Checking your cart…", current="")
            sync_info = None
            cart = read_carts(page, say)
            if cart is not None:
                order = db.get_order(conn, oid)
                items, extras = reconcile(order["items"], cart)
                db.save_order_items(conn, oid, items)
                sync_info = {"ok": True, "at": time.time(), "cart_count": len(cart),
                             "matched": sum(1 for i in items if i["added"]),
                             "extras": [e["title"] or e["asin"] for e in extras][:30]}
            msg = f"Your cart is ready for review: added {added} of {len(todo)} items."
            extra = {"sync": sync_info} if sync_info else {}
            say("done", msg, current="", added=added, total=len(todo), skipped=skipped,
                finished=time.time(), **extra)
            if interactive:
                input("Press Enter to close the browser... ")
            elif stay_open:
                # Leave the cart up briefly, then close so the app can keep checking your cart
                # in the background (the sign-in profile can only be open in one window).
                page.wait_for_timeout(8000)
            ctx.close()
    except Exception as e:
        say("error", f"Stopped: {e.__class__.__name__}: {str(e)[:200]}")
        return 1
    return 0


# ---------------- reading the real cart ----------------

def asin_from_url(url: str) -> str | None:
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url or "", re.I)
    return m.group(1).upper() if m else None


def _text(loc) -> str:
    try:
        return loc.first.inner_text(timeout=1000).strip() if loc.count() else ""
    except Exception:
        return ""


def read_cart_page(page) -> list[dict]:
    """Items on the cart page currently loaded: [{asin, title, qty}]."""
    out, seen = [], set()
    rows = page.locator(CART_ITEMS)
    for i in range(min(rows.count(), 200)):
        row = rows.nth(i)
        asin = (row.get_attribute("data-asin") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin) or asin in seen:
            continue
        seen.add(asin)
        title = _text(row.locator(".sc-product-title, .a-truncate-full, .sc-grid-item-product-title, h4, a.a-link-normal"))
        qty = row.get_attribute("data-quantity") or _text(row.locator(
            "[data-a-selector='value'], .a-dropdown-prompt, input[name='quantityBox']")) or "1"
        m = re.search(r"\d+", qty)
        out.append({"asin": asin, "title": re.sub(r"\s+", " ", title)[:120], "qty": int(m.group()) if m else 1})
    return out


def read_carts(page, say) -> list[dict] | None:
    """Read the Amazon cart and the Whole Foods cart. None = Amazon needs the person first."""
    items: dict[str, dict] = {}
    for url in (CART_URL, WF_CART_URL):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
        except Exception:
            continue
        if needs_human(page) or is_signed_in(page) is False:
            return None
        for it in read_cart_page(page):
            items.setdefault(it["asin"], it)
    return list(items.values())


def _words(s: str) -> set[str]:
    from .grocery import normalize_name

    return {w for w in normalize_name(s).split() if len(w) > 2}


def reconcile(order_items: list[dict], cart: list[dict]) -> tuple[list[dict], list[dict]]:
    """Tick exactly the order items that are really in the cart.

    Matched by product ID when we know it; otherwise every word of the item's name must
    appear in a cart item's title ("baby spinach" ~ "365 Organic Baby Spinach, 5 oz").
    Returns (updated items, cart items that aren't on this order)."""
    by_asin = {c["asin"]: c for c in cart}
    used: set[str] = set()
    for it in order_items:
        asin = it.get("asin") or asin_from_url(it.get("url", ""))
        hit = None
        if asin and asin in by_asin:
            hit = by_asin[asin]
        else:  # unknown product, or you swapped in a different one in the cart
            want = _words(it["name"])
            if want:
                hit = next((c for c in cart if c["asin"] not in used and want <= _words(c["title"])), None)
        it["added"] = hit is not None
        it["in_cart_qty"] = hit["qty"] if hit else 0
        if hit:
            used.add(hit["asin"])
            it.setdefault("asin", hit["asin"])
    extras = [c for c in cart if c["asin"] not in used]
    return order_items, extras


def sync_cart(order_id: int, headless: bool = True, profile_dir: str | None = None) -> int:
    """Look at the real carts and update the order's ticks to match. If a product preview was
    asked for meanwhile (the browser can only do one thing at a time), run it straight after."""
    code = _sync_cart(order_id, headless, profile_dir)
    pending = (read_status(order_id) or {}).get("pending_preview")
    if pending:
        write_status(order_id, pending_preview=None)
        return preview_order(order_id, headless=not pending.get("show"), profile_dir=profile_dir,
                             refresh=bool(pending.get("refresh")))
    return code


def _sync_cart(order_id: int, headless: bool = True, profile_dir: str | None = None) -> int:
    from playwright.sync_api import sync_playwright

    conn = db.connect()
    db.init(conn)
    order = db.get_order(conn, order_id)
    if not order:
        return 1
    prev = read_status(order_id) or {}
    write_status(order_id, state="syncing", pid=os.getpid(), message="Checking your Amazon cart…",
                 before_sync=prev.get("state") if prev.get("state") not in ACTIVE_STATES else prev.get("before_sync"))
    profile = profile_dir or str(Path(db.db_path()).parent / "browser-profile")

    def done(**fields):
        before = (read_status(order_id) or {}).get("before_sync")
        write_status(order_id, state=before if before in ("done",) else "idle", pid=None, **fields)

    try:
        with sync_playwright() as pw:
            ctx = _launch(pw, profile, headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            cart = read_carts(page, lambda *a, **k: None)
            ctx.close()
    except Exception as e:
        done(sync={"ok": False, "at": time.time(),
                   "error": f"Couldn't open the browser ({e.__class__.__name__}). Is the cart filler "
                            "window still open? Close it and try again."}, message="")
        return 1
    if cart is None:
        done(sync={"ok": False, "at": time.time(), "error": "Amazon wants you to sign in (or confirm "
                   "you're human) before I can read your cart. Use Fill my cart or Sync (show browser)."},
             message="")
        return 1

    order = db.get_order(conn, order_id)  # re-read; you may have ticked things meanwhile
    items, extras = reconcile(order["items"], cart)
    db.save_order_items(conn, order_id, items)
    in_cart = sum(1 for i in items if i["added"])
    fields = {"sync": {"ok": True, "at": time.time(), "cart_count": len(cart), "matched": in_cart,
                       "extras": [e["title"] or e["asin"] for e in extras][:30]}, "message": ""}
    if in_cart == 0:
        # Cart was emptied: forget the old "ready for review" summary.
        fields.update(added=0, skipped=[], screenshots={})
        write_status(order_id, state="idle", pid=None, before_sync=None, **fields)
    else:
        done(**fields)
    print(f"Cart has {len(cart)} item(s); {in_cart} of {len(items)} order items are in it.")
    return 0


# ---------------- product preview ----------------

_PRICE = re.compile(r"\$\s?(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)")
_UNIT_PRICE = re.compile(r"\(\s*(\$\s?[\d.,]+\s*/\s*[^)]{1,24})\)")


def _price(text: str) -> float | None:
    m = _PRICE.search(text or "")
    return float(m.group(1).replace(",", "")) if m else None


def search_url(name: str) -> str:
    if SEARCH_BASE:
        from urllib.parse import quote_plus
        return f"{SEARCH_BASE}?k={quote_plus(ordering.search_term(name))}"
    return ordering.whole_foods_search_url(name)


def read_results(page, limit: int = PREVIEW_OPTIONS) -> list[dict]:
    """Non-sponsored search results: [{asin, title, image, price, unit_price}]."""
    out = []
    cards = page.locator(RESULT_CARDS)
    for i in range(min(cards.count(), 16)):
        card = cards.nth(i)
        try:
            text = card.inner_text(timeout=2000)
        except Exception:
            continue
        asin = (card.get_attribute("data-asin") or "").strip().upper()
        if re.search(r"\bSponsored\b", text) or not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue
        img = card.locator("img.s-image, img")
        image = (img.first.get_attribute("src") or "") if img.count() else ""
        price_text = _text(card.locator(".a-price:not(.a-text-price) .a-offscreen, .a-price .a-offscreen"))
        unit = _UNIT_PRICE.search(text)
        out.append({"asin": asin, "title": re.sub(r"\s+", " ", _text(card.locator("h2")))[:140],
                    "image": image if image.startswith(("https://", "http://")) else "",
                    "price": _price(price_text) or _price(text),
                    "unit_price": re.sub(r"\s+", "", unit.group(1)) if unit else ""})
        if len(out) >= limit:
            break
    return out


_TITLE_JUNK = re.compile(r"^\s*amazon\.com\s*:\s*|\s*[:|-]\s*(amazon\.com|grocery[^:|]*|whole foods[^:|]*)\s*$", re.I)


def _meta(page, prop: str) -> str:
    try:
        loc = page.locator(f"meta[property='{prop}'], meta[name='{prop}']")
        return (loc.first.get_attribute("content") or "").strip() if loc.count() else ""
    except Exception:
        return ""


def read_product(page) -> dict | None:
    """The product page currently loaded (for your saved pick). Only product-specific
    elements are trusted: a page's first heading can be anything (e.g. a chat panel)."""
    asin = asin_from_url(page.url or "")
    if not asin:
        return None
    title = _text(page.locator("#productTitle, #title #productTitle, #title"))
    if not title:
        title = _meta(page, "og:title")
    if not title:
        try:
            title = page.title() or ""
        except Exception:
            title = ""
        for _ in range(2):
            title = _TITLE_JUNK.sub("", title).strip()
        if title.lower() in ("amazon.com", "amazon", ""):
            title = ""
    img = page.locator("#landingImage, #imgTagWrapperId img, #main-image")
    image = (img.first.get_attribute("src") or "") if img.count() else ""
    image = image or _meta(page, "og:image")
    return {"asin": asin, "title": re.sub(r"\s+", " ", title)[:140],
            "image": image if image.startswith(("https://", "http://")) else "",
            "price": _price(_text(page.locator(
                "#corePrice_feature_div .a-offscreen, #corePriceDisplay_desktop_feature_div .a-offscreen, "
                "#apex_desktop .a-price .a-offscreen, .a-price .a-offscreen"))),
            "unit_price": "", "pick": True}


class _Blocked(Exception):
    """Amazon wants a sign-in or a human check that a hidden browser can't show you."""


def _preview_item(page, item: dict, headless: bool, say) -> list[dict]:
    """Options for one item: your saved product first (always, even if its page can't be
    read), then the top non-sponsored search results."""
    def check():
        if needs_human(page) and (headless or not wait_for_human(page, say)):
            raise _Blocked()

    options = []
    asin = asin_from_url(item.get("url", "")) if item.get("has_pick") else None
    if asin:
        mine = None
        try:
            page.goto(item["url"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(800)
            check()
            mine = read_product(page)
        except _Blocked:
            raise
        except Exception as e:
            print(f"  {item['name']}: couldn't read your saved product ({e.__class__.__name__})")
        mine = mine or {"asin": asin, "title": "", "image": "", "price": None, "unit_price": "", "pick": True}
        mine["asin"] = asin  # the product we'll add, even if Amazon redirected
        mine["title"] = mine["title"] or re.sub(r"^auto-picked:\s*", "", item.get("pick_label") or "") \
            or "Your saved product"
        options.append(mine)
    try:
        page.goto(search_url(item["name"]), wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1000)
        check()
        seen = {o["asin"] for o in options}
        options += [o for o in read_results(page) if o["asin"] not in seen]
    except _Blocked:
        raise
    except Exception as e:
        if not options:
            raise
        print(f"  {item['name']}: search failed ({e.__class__.__name__}); showing your saved product only")
    return options[:PREVIEW_OPTIONS + 1]


def preview_order(order_id: int, headless: bool = True, profile_dir: str | None = None,
                  refresh: bool = False) -> int:
    """Look up product options (photo, name, price) for each item, for the order page."""
    from playwright.sync_api import sync_playwright

    conn = db.connect()
    db.init(conn)
    order = db.get_order(conn, order_id)
    if not order:
        return 1
    todo = [i["key"] for i in order["items"] if refresh or "options" not in i]
    prev = read_status(order_id) or {}
    before = prev.get("state") if prev.get("state") not in ACTIVE_STATES else prev.get("before_preview")
    write_status(order_id, state="previewing", pid=os.getpid(), before_preview=before,
                 message="Looking up products…", preview={"done": 0, "total": len(todo)})

    def finish(**preview):
        write_status(order_id, state=before if before == "done" else "idle", pid=None, message="",
                     preview=dict(preview, at=time.time()))

    def say(_state, message, **_extra):  # wait_for_human's messages, shown on the order page
        write_status(order_id, message=message)

    profile = profile_dir or str(Path(db.db_path()).parent / "browser-profile")
    found = 0
    try:
        with sync_playwright() as pw:
            ctx = _launch(pw, profile, headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            failed, streak = [], 0
            for n, key in enumerate(todo, 1):
                item = next((i for i in db.get_order(conn, order_id)["items"] if i["key"] == key), None)
                if not item:
                    continue
                write_status(order_id, message=f"Looking up {item['name']} ({n} of {len(todo)})…",
                             preview={"done": n - 1, "total": len(todo)})
                try:
                    options = _preview_item(page, item, headless, say)
                except _Blocked:
                    ctx.close()
                    finish(ok=False, error=_PREVIEW_BLOCKED, found=found, looked_up=n - 1)
                    return 1
                except Exception as e:  # one bad page shouldn't end the whole preview
                    print(f"  {item['name']}: {e.__class__.__name__}: {str(e)[:120]}")
                    failed.append(item["name"])
                    streak += 1
                    if streak >= FAIL_STREAK_PAUSE:
                        break
                    continue
                streak = 0
                if options:
                    found += 1
                order = db.get_order(conn, order_id)  # re-read: you may be ticking things meanwhile
                for i in order["items"]:
                    if i["key"] == key:
                        i["options"] = options
                db.save_order_items(conn, order_id, order["items"])
                page.wait_for_timeout(int(random.uniform(*PAUSE_BETWEEN_ITEMS) * 1000))
            shot = _snapshot(page, order_id, "preview") if todo and not found else None
            ctx.close()
    except Exception as e:
        finish(ok=False, found=found, error=f"Stopped: {e.__class__.__name__}: {str(e)[:150]}")
        return 1
    if streak >= FAIL_STREAK_PAUSE:
        finish(ok=False, found=found, looked_up=len(todo), failed=failed,
               error=f"Stopped after {streak} items in a row couldn't be loaded ({', '.join(failed[-streak:])}). "
                     "Amazon may be slow — press the button again to carry on where it stopped.")
        return 1
    if todo and not found:
        # Nothing readable at all: say so, with a picture of what Amazon showed.
        finish(ok=False, found=0, looked_up=len(todo), shot=shot,
               error="I couldn't read any products from Amazon's search results. Amazon may show "
                     "a hidden browser a different page — try it with the browser showing.")
        return 1
    finish(ok=True, found=found, looked_up=len(todo), failed=failed)
    print(f"Found products for {found} of {len(todo)} item(s).")
    return 0


_PREVIEW_BLOCKED = ("Amazon wants you to sign in or confirm you're human first. Use "
                    "\"Preview with the browser showing\" and deal with it in the window.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--order", type=int, help="order id (default: latest unplaced order)")
    p.add_argument("--profile", help="browser profile directory")
    p.add_argument("--limit", type=int, help="only try the first N items")
    p.add_argument("--no-prompt", action="store_true", help="don't wait for Enter (used by the web app)")
    p.add_argument("--no-learn", action="store_true", help="don't remember the products it picks")
    p.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--sync", action="store_true", help="read your Amazon cart and update the ticks")
    p.add_argument("--show", action="store_true", help="with --sync/--preview: show the browser window")
    p.add_argument("--preview", action="store_true", help="look up product photos and prices")
    p.add_argument("--refresh", action="store_true", help="with --preview: look up every item again")
    a = p.parse_args(argv)
    if not playwright_available():
        print("Needs Playwright:  pip install playwright && python -m playwright install chromium")
        return 2
    if a.preview:
        order = pick_order(db.connect(), a.order)
        return preview_order(order["id"], headless=not a.show, profile_dir=a.profile,
                             refresh=a.refresh) if order else 1
    if a.sync:
        order = pick_order(db.connect(), a.order)
        return sync_cart(order["id"], headless=not a.show, profile_dir=a.profile) if order else 1
    return fill_cart(a.order, interactive=not a.no_prompt, learn=not a.no_learn, limit=a.limit,
                     headless=a.headless, profile_dir=a.profile, stay_open=not a.headless)


if __name__ == "__main__":
    sys.exit(main())


def has_profile() -> bool:
    """True once the cart filler has been used (so there's an Amazon sign-in to reuse)."""
    return (Path(db.db_path()).parent / "browser-profile").is_dir()
