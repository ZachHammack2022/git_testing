import os

import pytest

os.environ["RECIPE_PLANNER_DISABLE_AI"] = "1"

from recipe_planner import db  # noqa: E402
from recipe_planner.app import create_app  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def app(tmp_path):
    a = create_app(str(tmp_path / "app.db"))
    a.config["TESTING"] = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()
