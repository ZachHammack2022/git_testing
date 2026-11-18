"""End-to-end: run the cart filler in a real (headless) browser against a fake Amazon."""
import glob
import http.server
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from recipe_planner import autocart, db

pytest.importorskip("playwright.sync_api")
BROWSERS = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")

CLICKS = []

HOME = "<div id='nav-link-accountList-nav-line-1'>Hello, Sam</div>"


def card(asin, title, sponsored=False, button=True):
    btn = (f"<button onclick=\"fetch('/added?asin={asin}')\">Add to cart</button>" if button else "")
    return (f"<div data-component-type='s-search-result' data-asin='{asin}'>"
            f"{'<span>Sponsored</span>' if sponsored else ''}<h2><a href='/dp/{asin}'>{title}</a></h2>{btn}</div>")


def search_page(q):
    if q == "lime":  # result without an add button: must open the product page
        return card("B0LIMEXXXX", "Organic Limes", button=False)
    if q == "nothing":
        return "<p>No results</p>"
    asin = ("B0" + q.upper() + "XXXXXXXX")[:10]  # real ASINs are 10 characters
    return card("B0SPONSORX", "Sponsored Thing", sponsored=True) + card(asin, f"365 {q}")


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/added":
            CLICKS.append(parse_qs(u.query)["asin"][0])
            body = "ok"
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
            body = f"<button id='add-to-cart-button' onclick=\"fetch('/added?asin={asin}')\">Add to Cart</button>"
        elif u.path == "/never":
            CLICKS.append("auto-clicked-checkpoint")
            body = HOME
        else:
            body = HOME
        data = f"<html><body>{body}</body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(data)


@pytest.mark.skipif(not BROWSERS, reason="no local Chromium")
def test_fill_cart_end_to_end(tmp_path, monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("RECIPE_DB", str(tmp_path / "e2e.db"))
    monkeypatch.setenv("AUTOCART_BROWSER", BROWSERS[0])
    monkeypatch.setattr(autocart, "AMAZON_HOME", base + "/")
    monkeypatch.setattr(autocart, "CART_URL", base + "/cart")

    conn = db.connect()
    db.init(conn)
    items = [{"key": k, "name": k, "amount": "1", "category": "produce", "recipes": [],
              "url": f"{base}/s?k={k}", "has_pick": False, "pick_label": "", "added": False}
             for k in ["avocado", "lime", "nothing", "egg"]]
    items.append({"key": "honey", "name": "honey", "amount": "", "category": "pantry", "recipes": [],
                  "url": f"{base}/dp/B0HONEY", "has_pick": True, "pick_label": "", "added": False})
    oid = db.create_order(conn, None, items)

    rc = autocart.fill_cart(oid, interactive=False, headless=True, stay_open=False,
                            profile_dir=str(tmp_path / "profile"))
    srv.shutdown()
    assert rc == 0
    assert "checkpoint-shown" in CLICKS and "auto-clicked-checkpoint" not in CLICKS
    assert "B0SPONSORX" not in CLICKS                      # skipped the sponsored result
    assert {"B0AVOCADOX", "B0LIMEXXXX", "B0HONEY", "B0EGGXXXXX"} <= set(CLICKS)
    order = db.get_order(conn, oid)
    added = {i["key"]: i["added"] for i in order["items"]}
    assert added == {"avocado": True, "lime": True, "nothing": False, "honey": True, "egg": True}
    picks = db.product_picks(conn)
    assert "/dp/B0AVOCADOX?almBrandId=" in picks["avocado"]["url"] and "365 avocado" in picks["avocado"]["label"]
    assert "/dp/B0LIMEXXXX?almBrandId=" in picks["lime"]["url"]
    st = autocart.read_status(oid)
    assert st["state"] == "done" and st["skipped"] == ["nothing"]
