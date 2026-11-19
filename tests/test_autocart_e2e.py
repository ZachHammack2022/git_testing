"""End-to-end: run the cart filler in a real (headless) browser against a fake Amazon."""
import glob
import http.server
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

from recipe_planner import autocart, db

pytest.importorskip("playwright.sync_api")
BROWSERS = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")

CLICKS = []
STATE = {"home": "signed_in", "no_buttons": False, "cart": {}}
GREETING = "<div id='nav-link-accountList-nav-line-1'>{}</div>"


def home_page():
    if STATE["home"] == "signed_in":
        return GREETING.format("Hello, Sam")
    if STATE["home"] == "signed_out":
        # A real sign-in navigates back to Amazon; simulate that with a periodic reload.
        return "<meta http-equiv='refresh' content='1'>" + GREETING.format("Hello, sign in")
    return "<p>Some page layout we don't recognize</p>"


def card(asin, title, sponsored=False, button=True):
    button = button and not STATE["no_buttons"]
    btn = (f"<button onclick=\"fetch('/added?asin={asin}')\">Add to cart</button>" if button else "")
    return (f"<div data-component-type='s-search-result' data-asin='{asin}'>"
            f"{'<span>Sponsored</span>' if sponsored else ''}<img class='s-image' src='https://m.media-amazon.com/images/I/{asin}.jpg'>"
            f"<h2><a href='/dp/{asin}'>{title}</a></h2><span class='a-price'><span class='a-offscreen'>$3.49</span></span>"
            f"<span>($0.70/Ounce)</span>{btn}</div>")


def search_page(q):
    if q == "lime":  # result without an add button: must open the product page
        return card("B0LIMEXXXX", "Organic Limes", button=False)
    if q == "nothing":
        return "<p>No results</p>"
    if q == "grapes":
        return "".join(card(f"B0GRAPE{n:03d}", f"Grapes option {n}") for n in range(1, 9))
    asin = ("B0" + q.upper() + "XXXXXXXX")[:10]  # real ASINs are 10 characters
    return card("B0SPONSORX", "Sponsored Thing", sponsored=True) + card(asin, f"365 {q}")


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/added":
            asin = parse_qs(u.query)["asin"][0]
            CLICKS.append(asin)
            STATE["cart"][asin] = f"Product {asin}"
            body = "ok"
        elif u.path in ("/cart", "/cart/localmarket"):
            rows = "".join(f"<div class='sc-list-item' data-asin='{a}'><span class='sc-product-title'>{t}</span>"
                           f"<span data-a-selector='value'>1</span></div>" for a, t in STATE["cart"].items())
            body = home_page() + f"<div id='sc-active-cart'>{rows}</div>"
        elif u.path == "/s" and parse_qs(u.query)["k"][0] == "egg" and "passed" not in u.query:
            self.send_response(302)  # Amazon-style soft bot check on the way
            self.send_header("Location", "/checkpoint/continue?next=/s%3Fk%3Degg%26passed%3D1")
            self.end_headers()
            return
        elif u.path.startswith("/checkpoint/"):
            # The helper must NOT click through this itself; it waits for the person. Here the
            # "person" finishes the check after a few seconds (simulated with a meta refresh).
            nxt = parse_qs(u.query)["next"][0]
            CLICKS.append("checkpoint-shown")
            body = (f"<meta http-equiv='refresh' content='4;url={nxt}'>"
                    f"<p>To continue, please click the button below.</p><a href='/never'><button>Continue shopping</button></a>")
        elif u.path == "/s":
            body = search_page(parse_qs(u.query)["k"][0])
        elif u.path.startswith("/dp/"):
            asin = u.path.split("/")[-1]
            body = (f"<span id='productTitle'>Picked product {asin}</span>"
                    f"<span class='a-price'><span class='a-offscreen'>$5.99</span></span>")
            body += ("" if STATE["no_buttons"] else
                     f"<button id='add-to-cart-button' onclick=\"fetch('/added?asin={asin}')\">Add to Cart</button>")
        elif u.path == "/never":
            CLICKS.append("auto-clicked-checkpoint")
            body = home_page()
        else:
            body = home_page()
        data = f"<html><body>{body}</body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_amazon(tmp_path, monkeypatch):
    CLICKS.clear()
    STATE.update(home="signed_in", no_buttons=False, cart={})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("RECIPE_DB", str(tmp_path / "e2e.db"))
    monkeypatch.setenv("AUTOCART_BROWSER", BROWSERS[0] if BROWSERS else "")
    monkeypatch.setattr(autocart, "AMAZON_HOME", base + "/")
    monkeypatch.setattr(autocart, "CART_URL", base + "/cart")
    monkeypatch.setattr(autocart, "WF_CART_URL", base + "/cart/localmarket")
    monkeypatch.setattr(autocart, "PAUSE_BETWEEN_ITEMS", (0.1, 0.2))
    monkeypatch.setattr(autocart, "SEARCH_BASE", base + "/s")
    yield base
    srv.shutdown()


def _order(base, keys):
    conn = db.connect()
    db.init(conn)
    items = [{"key": k, "name": k, "amount": "1", "category": "produce", "recipes": [],
              "url": f"{base}/s?k={k}", "has_pick": False, "pick_label": "", "added": False}
             for k in keys]
    return conn, db.create_order(conn, None, items)


def _run(oid, tmp_path):
    return autocart.fill_cart(oid, interactive=False, headless=True, stay_open=False,
                              profile_dir=str(tmp_path / "profile"))


@pytest.mark.skipif(not BROWSERS, reason="no local Chromium")
def test_waits_for_sign_in(fake_amazon, tmp_path):
    STATE["home"] = "signed_out"
    conn, oid = _order(fake_amazon, ["avocado"])
    seen = []

    def person():  # watches the status, then "signs in"
        for _ in range(100):
            st = autocart.read_status(oid) or {}
            if st.get("state") == "signin":
                seen.append("signin")
                assert CLICKS == []  # nothing attempted while signed out
                time.sleep(2)
                STATE["home"] = "signed_in"
                return
            time.sleep(0.2)
    t = threading.Thread(target=person)
    t.start()
    assert _run(oid, tmp_path) == 0
    t.join()
    assert seen == ["signin"] and "B0AVOCADOX" in CLICKS


@pytest.mark.skipif(not BROWSERS, reason="no local Chromium")
def test_go_override_when_greeting_unrecognized(fake_amazon, tmp_path):
    STATE["home"] = "unknown"
    conn, oid = _order(fake_amazon, ["avocado"])

    def press_go():
        for _ in range(100):
            if (autocart.read_status(oid) or {}).get("state") == "signin":
                autocart.write_status(oid, go=True)
                return
            time.sleep(0.2)
    threading.Thread(target=press_go).start()
    assert _run(oid, tmp_path) == 0
    assert "B0AVOCADOX" in CLICKS


@pytest.mark.skipif(not BROWSERS, reason="no local Chromium")
def test_stops_early_with_screenshots_when_nothing_adds(fake_amazon, tmp_path):
    STATE["no_buttons"] = True
    conn, oid = _order(fake_amazon, ["avocado", "kale", "onion", "garlic", "rice"])
    assert _run(oid, tmp_path) == 1
    st = autocart.read_status(oid)
    assert st["state"] == "error" and "couldn't be added" in st["message"]
    assert st["skipped"] == ["avocado", "kale", "onion"]          # stopped after 3, not all 5
    assert set(st["screenshots"]) == {"avocado", "kale", "onion"}
    assert (autocart.debug_dir(oid) / st["screenshots"]["kale"]).is_file()


STATES_SEEN = set()


@pytest.mark.skipif(not BROWSERS, reason="no local Chromium")
def test_fill_cart_end_to_end(fake_amazon, tmp_path, monkeypatch):
    base = fake_amazon
    real_write = autocart.write_status

    def spy(order_id, **fields):
        if "state" in fields:
            STATES_SEEN.add(fields["state"])
        real_write(order_id, **fields)
    monkeypatch.setattr(autocart, "write_status", spy)

    conn = db.connect()
    db.init(conn)
    items = [{"key": k, "name": k, "amount": "1", "category": "produce", "recipes": [],
              "url": f"{base}/s?k={k}", "has_pick": False, "pick_label": "", "added": False}
             for k in ["avocado", "lime", "nothing", "egg"]]
    items.append({"key": "honey", "name": "honey", "amount": "", "category": "pantry", "recipes": [],
                  "url": f"{base}/dp/B0HONEYXXX", "has_pick": True, "pick_label": "", "added": False})
    oid = db.create_order(conn, None, items)

    rc = autocart.fill_cart(oid, interactive=False, headless=True, stay_open=False,
                            profile_dir=str(tmp_path / "profile"))
    assert rc == 0
    assert "checkpoint-shown" in CLICKS and "auto-clicked-checkpoint" not in CLICKS
    assert "human" in STATES_SEEN
    assert "B0SPONSORX" not in CLICKS                      # skipped the sponsored result
    assert {"B0AVOCADOX", "B0LIMEXXXX", "B0HONEYXXX", "B0EGGXXXXX"} <= set(CLICKS)
    order = db.get_order(conn, oid)
    added = {i["key"]: i["added"] for i in order["items"]}
    assert added == {"avocado": True, "lime": True, "nothing": False, "honey": True, "egg": True}
    picks = db.product_picks(conn)
    assert "/dp/B0AVOCADOX?almBrandId=" in picks["avocado"]["url"] and "365 avocado" in picks["avocado"]["label"]
    assert "/dp/B0LIMEXXXX?almBrandId=" in picks["lime"]["url"]
    st = autocart.read_status(oid)
    assert st["state"] == "done" and st["skipped"] == ["nothing"]
    assert st["added"] == 4 and st["total"] == 5 and "ready for review" in st["message"]
    assert st["sync"]["ok"] and st["sync"]["cart_count"] == 4   # checked the real cart at the end


@pytest.mark.skipif(not BROWSERS, reason="no local Chromium")
def test_sync_follows_the_real_cart(fake_amazon, tmp_path):
    base = fake_amazon
    conn = db.connect()
    db.init(conn)
    items = [{"key": k, "name": k, "amount": "1", "category": "produce", "recipes": [],
              "url": f"https://www.amazon.com/dp/{asin}", "has_pick": True, "pick_label": "", "added": True}
             for k, asin in [("avocado", "B0AVOCADOX"), ("kale", "B0KALEXXXX")]]
    items.append({"key": "baby spinach", "name": "baby spinach", "amount": "", "category": "produce",
                  "recipes": [], "url": "https://www.amazon.com/s?k=spinach", "has_pick": False,
                  "pick_label": "", "added": False})
    oid = db.create_order(conn, None, items)
    autocart.write_status(oid, state="done", added=2, total=3, message="ready")

    # You removed kale on Amazon and added spinach and coffee yourself.
    STATE["cart"] = {"B0AVOCADOX": "Hass Avocado", "B0SPINACH1": "365 Organic Baby Spinach 5oz",
                     "B0COFFEE11": "Coffee Beans"}
    assert autocart.sync_cart(oid, headless=True, profile_dir=str(tmp_path / "p")) == 0
    got = {i["key"]: i["added"] for i in db.get_order(conn, oid)["items"]}
    assert got == {"avocado": True, "kale": False, "baby spinach": True}
    st = autocart.read_status(oid)
    assert st["state"] == "done" and st["sync"]["extras"] == ["Coffee Beans"]

    # You emptied the cart.
    STATE["cart"] = {}
    assert autocart.sync_cart(oid, headless=True, profile_dir=str(tmp_path / "p")) == 0
    assert not any(i["added"] for i in db.get_order(conn, oid)["items"])
    st = autocart.read_status(oid)
    assert st["state"] == "idle" and st["sync"]["cart_count"] == 0 and st["skipped"] == []


@pytest.mark.skipif(not BROWSERS, reason="no local Chromium")
def test_sync_signed_out_reports_instead_of_unticking(fake_amazon, tmp_path):
    STATE["home"] = "signed_out"
    conn = db.connect()
    db.init(conn)
    oid = db.create_order(conn, None, [{"key": "kale", "name": "kale", "url": "https://www.amazon.com/dp/B0KALEXXXX",
                                        "has_pick": True, "added": True}])
    assert autocart.sync_cart(oid, headless=True, profile_dir=str(tmp_path / "p")) == 1
    assert db.get_order(conn, oid)["items"][0]["added"] is True   # didn't wipe ticks
    assert "sign in" in autocart.read_status(oid)["sync"]["error"]


@pytest.mark.skipif(not BROWSERS, reason="no local Chromium")
def test_preview_reads_options_and_your_pick(fake_amazon, tmp_path):
    conn, oid = _order(fake_amazon, ["grapes", "egg", "nothing"])
    order = db.get_order(conn, oid)
    order["items"][1].update(url=f"{fake_amazon}/dp/B0PICKEDXX", has_pick=True)  # your saved product
    db.save_order_items(conn, oid, order["items"])
    autocart.write_status(oid, state="done", added=2)

    assert autocart.preview_order(oid, headless=True, profile_dir=str(tmp_path / "profile")) == 1
    st = autocart.read_status(oid)
    # "egg" hits Amazon's bot check: a headless preview stops and says so instead of clicking.
    assert st["preview"]["ok"] is False and "browser showing" in st["preview"]["error"]
    assert st["state"] == "done"  # the "cart ready" card is left alone
    assert "auto-clicked-checkpoint" not in CLICKS
    grapes = db.get_order(conn, oid)["items"][0]["options"]
    assert len(grapes) == autocart.PREVIEW_OPTIONS
    assert grapes[0] == {"asin": "B0GRAPE001", "title": "Grapes option 1",
                         "image": "https://m.media-amazon.com/images/I/B0GRAPE001.jpg",
                         "price": 3.49, "unit_price": "$0.70/Ounce"}

    # Get past the check (as if the person did), then finish the rest.
    order = db.get_order(conn, oid)
    order["items"][1]["name"] = "eggs"
    db.save_order_items(conn, oid, order["items"])
    assert autocart.preview_order(oid, headless=True, profile_dir=str(tmp_path / "profile")) == 0
    items = db.get_order(conn, oid)["items"]
    assert items[0]["options"] == grapes  # already looked up: not repeated
    eggs = items[1]["options"]
    assert eggs[0]["pick"] and eggs[0]["asin"] == "B0PICKEDXX" and eggs[0]["price"] == 5.99
    assert eggs[1]["asin"] == "B0EGGSXXXX"  # sponsored result skipped
    assert items[2]["options"] == []
    st = autocart.read_status(oid)
    assert st["preview"]["ok"] and st["preview"]["found"] == 1 and st["state"] == "done"
    assert not CLICKS or set(CLICKS) <= {"checkpoint-shown"}  # looked, never added anything
