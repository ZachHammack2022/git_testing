"""Claude integration with the API client mocked out."""
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from recipe_planner import ai, db, planner


class FakeMessages:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps(payload))])


@pytest.fixture
def fake(monkeypatch):
    def install(*payloads):
        msgs = FakeMessages(payloads)
        monkeypatch.setattr(ai, "_client", SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
        monkeypatch.setattr(ai, "available", lambda: True)
        return msgs
    return install


NEW_RECIPE = {
    "title": "Crispy Gochujang Salmon", "description": "d", "cuisine": "Korean", "protein": "fish",
    "servings": 2, "prep_minutes": 25,
    "ingredients": [{"name": "salmon fillets", "quantity": 2, "unit": "fillet", "category": "meat & seafood"},
                    {"name": "gochujang", "quantity": 2, "unit": "tbsp", "category": "pantry"}],
    "steps": ["Glaze", "Roast"], "tags": ["new"],
}


def test_ai_plan_mixes_reused_and_new(conn, fake):
    db.save_settings(conn, {"cook_days": ["Mon", "Tue", "Wed"], "dinners_per_week": 3})
    msgs = fake({"summary": "More fish as requested.",
                 "reused": [{"day": "Mon", "recipe_id": 1}, {"day": "Mon", "recipe_id": 2},
                            {"day": "Tue", "recipe_id": 99999}],
                 "new": [{"day": "Wed", "recipe": NEW_RECIPE}]})
    pid = planner.plan_week(conn, dt.date(2026, 11, 23), use_ai=True)
    plan = db.get_plan(conn, pid)
    assert plan["planner"] == "claude" and plan["notes"] == "More fish as requested."
    by_day = {m["day"]: m for m in plan["meals"]}
    assert by_day["Mon"]["recipe_id"] == 1                      # duplicate day dropped
    assert by_day["Wed"]["recipe"]["title"] == NEW_RECIPE["title"]
    assert by_day["Wed"]["recipe"]["source"] == "ai"
    assert "Tue" in by_day                                      # invalid id backfilled by heuristic
    call = msgs.calls[0]
    assert call["model"] == ai.MODEL
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "Recipe library" in call["messages"][0]["content"]


def test_ai_failure_falls_back(conn, fake):
    import anthropic
    import httpx2 as httpx
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    fake(anthropic.APIConnectionError(request=req))
    pid = planner.plan_week(conn, dt.date(2026, 11, 23), use_ai=True)
    plan = db.get_plan(conn, pid)
    assert plan["planner"] == "heuristic" and "failed" in plan["notes"]
    assert len(plan["meals"]) == 5


def test_feedback_in_prompt(conn, fake):
    pid = db.create_plan(conn, "2026-11-16", [{"day": "Mon", "recipe_id": 1, "servings": 2}], "heuristic")
    m = db.get_plan(conn, pid)["meals"][0]
    db.save_feedback(conn, m["id"], 1, 2, "tweak", "way too salty")
    db.save_settings(conn, {"dislikes": "olives"})
    text = ai.taste_context(conn, db.get_settings(conn))
    assert "way too salty" in text and "olives" in text and "2/5" in text


def test_ai_swap_new_recipe(conn, fake):
    pid = planner.plan_week(conn, dt.date(2026, 11, 23), use_ai=False)
    meal = db.get_plan(conn, pid)["meals"][0]
    fake({"reason": "Lighter fish night.", "reuse_recipe_id": 0, "recipe": NEW_RECIPE})
    msg = planner.swap_meal(conn, meal["id"], "something lighter", use_ai=True)
    assert msg == "Lighter fish night."
    assert db.get_recipe(conn, db.get_meal(conn, meal["id"])["recipe_id"])["title"] == NEW_RECIPE["title"]


def test_same_title_not_duplicated(conn, fake):
    a = ai._store_new_recipe(conn, NEW_RECIPE)
    b = ai._store_new_recipe(conn, dict(NEW_RECIPE, title=NEW_RECIPE["title"].upper()))
    assert a == b


def test_ai_picks_with_disliked_ingredient_are_dropped(conn, fake):
    db.save_settings(conn, {"cook_days": ["Mon"], "dinners_per_week": 1, "dislikes": "gochujang"})
    fake({"summary": "s", "reused": [], "new": [{"day": "Mon", "recipe": NEW_RECIPE}]})
    pid = planner.plan_week(conn, dt.date(2026, 11, 23), use_ai=True, use_up=["salmon"])
    meal = db.get_plan(conn, pid)["meals"][0]
    assert "gochujang" not in " ".join(i["name"] for i in meal["recipe"]["ingredients"])
