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
