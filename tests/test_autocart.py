from recipe_planner import autocart, db


def test_pick_order_prefers_latest_open(conn):
    a = db.create_order(conn, None, [{"key": "x", "added": False}])
    b = db.create_order(conn, None, [{"key": "y", "added": False}])
    db.mark_order_placed(conn, b)
    assert autocart.pick_order(conn, None)["id"] == a
    assert autocart.pick_order(conn, b)["id"] == b


class FakeLocator:
    def __init__(self, visible):
        self.visible, self.clicked = visible, False

    def count(self):
        return 1 if self.visible else 0

    @property
    def first(self):
        return self

    def is_visible(self):
        return self.visible

    def click(self, timeout=None):
        self.clicked = True


class FakePage:
    def __init__(self, role_visible):
        self.role = FakeLocator(role_visible)

    def locator(self, sel):
        return FakeLocator(False)

    def get_by_role(self, role, name=None):
        return self.role

    def wait_for_timeout(self, ms):
        pass


def test_try_add():
    page = FakePage(True)
    assert autocart._try_add(page) and page.role.clicked
    assert not autocart._try_add(FakePage(False))
    assert autocart.ADD_BUTTON.match("Add to Cart") and not autocart.ADD_BUTTON.match("Add to List")


def test_whole_foods_product_url():
    from recipe_planner.ordering import whole_foods_product_url as f
    wf = "?almBrandId=VUZHIFdob2xlIEZvb2Rz&fpw=alm"
    assert f("https://www.amazon.com/Lime-Regular/dp/B07811WM8Z/ref=sr_1_1?x=1") == "https://www.amazon.com/dp/B07811WM8Z" + wf
    assert f("https://www.amazon.com/gp/product/b000nogkn4") == "https://www.amazon.com/dp/B000NOGKN4" + wf
    assert f("B000NOGKN4") == "https://www.amazon.com/dp/B000NOGKN4" + wf
    assert f("https://a.co/d/abc") == "https://a.co/d/abc"


def test_autofill_routes(client, app, monkeypatch):
    import re
    r = client.post("/plans", data={"week_start": "2026-11-23"})
    pid = int(re.search(r"/plans/(\d+)", r.headers["Location"]).group(1))
    oid = int(client.post(f"/plans/{pid}/order").headers["Location"].rsplit("/", 1)[1])

    monkeypatch.setattr(autocart, "playwright_available", lambda: False)
    page = client.post(f"/orders/{oid}/autofill", follow_redirects=True).get_data(as_text=True)
    assert "needs Playwright" in page

    started = {}
    monkeypatch.setattr(autocart, "playwright_available", lambda: True)
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: started.setdefault("args", args))
    monkeypatch.setenv("RECIPE_DB", app.config["DB_FILE"])
    client.post(f"/orders/{oid}/autofill")
    assert started["args"][-3:] == ["--order", str(oid), "--no-prompt"]
    st = client.get(f"/orders/{oid}/autofill").get_json()
    assert st["status"]["state"] == "starting" and st["added"] == []
    assert "Filling…" in client.get(f"/orders/{oid}").get_data(as_text=True)


def test_add_button_pattern():
    ok = ["Add", "Add to cart", "Add to Cart", "Add Organic Hass Avocado to cart", "add to your cart"]
    bad = ["Add to List", "Add to Registry", "Add a gift receipt", "Added", "Address"]
    assert all(autocart.ADD_BUTTON.match(t) for t in ok)
    assert not any(autocart.ADD_BUTTON.match(t) for t in bad)


def test_go_override_and_screenshot_route(client, app, monkeypatch, tmp_path):
    monkeypatch.setenv("RECIPE_DB", app.config["DB_FILE"])
    conn = db.connect(app.config["DB_FILE"])
    oid = db.create_order(conn, None, [{"key": "x", "name": "x", "added": False}])
    autocart.write_status(oid, state="signin", message="Sign in…")
    client.post(f"/orders/{oid}/autofill/go")
    assert autocart.read_status(oid)["go"] is True
    d = autocart.debug_dir(oid)
    d.mkdir(parents=True)
    (d / "kale.png").write_bytes(b"\x89PNG fake")
    assert client.get(f"/orders/{oid}/autofill/shot/kale.png").status_code == 200
    assert client.get(f"/orders/{oid}/autofill/shot/..%2Fsecret.png").status_code == 404
