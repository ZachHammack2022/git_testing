"""EXPERIMENTAL: fill your Amazon Whole Foods cart from an order, in a real browser.

    pip install playwright && python -m playwright install chromium
    python -m recipe_planner.autocart            # latest unplaced order
    python -m recipe_planner.autocart --order 3

A visible browser opens with its own profile (kept in instance/browser-profile), so you sign in
to Amazon once and stay signed in. For each unticked item it opens the item's link (your saved
product, or a Whole Foods search) and clicks the first "Add to cart" button it finds. Items it
adds are ticked in the app; anything it can't handle is left for you.

It never checks out. Review quantities and pick a delivery window yourself.
Amazon changes its pages often, so treat this as a helper, not a guarantee.
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
from pathlib import Path

from . import db

ADD_BUTTON = re.compile(r"^\s*(add to cart|add)\s*$", re.I)


def pick_order(conn, order_id: int | None) -> dict | None:
    if order_id:
        return db.get_order(conn, order_id)
    row = conn.execute(
        "SELECT id FROM orders WHERE status != 'placed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return db.get_order(conn, row["id"]) if row else None


def _try_add(page) -> bool:
    """Click the first visible add-to-cart control on the page. True if one was clicked."""
    candidates = [
        page.locator("#freshAddToCartButton input, #freshAddToCartButton button"),
        page.locator("#add-to-cart-button"),
        page.get_by_role("button", name=ADD_BUTTON),
        page.locator("input[name='submit.addToCart'], button[name='submit.addToCart']"),
    ]
    for loc in candidates:
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=5000)
                page.wait_for_timeout(1500)
                return True
        except Exception:  # selector missing, detached, covered, etc.
            continue
    return False


def run(order_id: int | None = None, profile_dir: str | None = None, limit: int | None = None) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Needs Playwright:  pip install playwright && python -m playwright install chromium")
        return 2

    conn = db.connect()
    db.init(conn)
    order = pick_order(conn, order_id)
    if not order:
        print("No open order found. Start one from the shopping list first.")
        return 1
    todo = [i for i in order["items"] if not i.get("added")]
    if limit:
        todo = todo[:limit]
    if not todo:
        print("Everything on this order is already ticked off.")
        return 0

    profile = profile_dir or str(Path(db.db_path()).parent / "browser-profile")
    added, skipped = 0, []
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(profile, headless=False, viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.amazon.com/")
        input("Sign in to Amazon in the browser window if needed (and set your Whole Foods "
              "delivery address), then press Enter here to start... ")

        for item in todo:
            print(f"- {item['name']} ({item.get('amount') or 'any'}) ... ", end="", flush=True)
            try:
                page.goto(item["url"], wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                ok = _try_add(page)
            except Exception as e:
                ok = False
                print(f"error: {e.__class__.__name__} ", end="")
            if ok:
                added += 1
                item["added"] = True
                db.save_order_items(conn, order["id"], order["items"])
                print("added")
            else:
                skipped.append(item["name"])
                print("skipped")
            time.sleep(random.uniform(1.5, 3.5))  # be gentle

        page.goto("https://www.amazon.com/gp/cart/view.html")
        print(f"\nAdded {added} of {len(todo)} items. Check quantities in the cart, then check out.")
        if skipped:
            print("Do these by hand:", ", ".join(skipped))
        input("Press Enter to close the browser... ")
        ctx.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--order", type=int, help="order id (default: latest unplaced order)")
    p.add_argument("--profile", help="browser profile directory")
    p.add_argument("--limit", type=int, help="only try the first N items")
    a = p.parse_args(argv)
    return run(a.order, a.profile, a.limit)


if __name__ == "__main__":
    sys.exit(main())
