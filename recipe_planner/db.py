"""SQLite storage. One file, plain SQL, JSON columns for lists."""
import json
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    cuisine TEXT DEFAULT '',
    protein TEXT DEFAULT '',
    servings INTEGER DEFAULT 2,
    prep_minutes INTEGER DEFAULT 30,
    ingredients TEXT NOT NULL,      -- JSON list of {name, quantity, unit, category}
    steps TEXT NOT NULL,            -- JSON list of strings
    tags TEXT DEFAULT '[]',         -- JSON list of strings
    source TEXT DEFAULT 'library',  -- library | ai | manual
    archived INTEGER DEFAULT 0,     -- 1 = never suggest again
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    week_start TEXT NOT NULL,       -- ISO date of Monday
    notes TEXT DEFAULT '',
    planner TEXT DEFAULT 'heuristic',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS plan_meals (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    day TEXT NOT NULL,              -- Mon..Sun
    recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    servings INTEGER NOT NULL,
    cooked INTEGER DEFAULT 0,
    locked INTEGER DEFAULT 0,
    leftovers INTEGER DEFAULT 0     -- extra servings to cook for lunch the next day
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id),
    plan_meal_id INTEGER REFERENCES plan_meals(id) ON DELETE SET NULL,
    rating INTEGER NOT NULL,        -- 1..5
    make_again TEXT DEFAULT 'yes',  -- yes | tweak | no
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL             -- JSON
);

CREATE TABLE IF NOT EXISTS pantry (
    name TEXT PRIMARY KEY           -- normalized ingredient name always on hand
);

CREATE TABLE IF NOT EXISTS product_picks (
    name TEXT PRIMARY KEY,          -- normalized ingredient name
    url TEXT NOT NULL,              -- preferred Amazon/Whole Foods product URL
    label TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL,
    items TEXT NOT NULL,            -- JSON list of shopping items (+ 'added' flag)
    status TEXT DEFAULT 'building', -- building | placed
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    placed_at TEXT
);
"""

DEFAULT_SETTINGS = {
    "household_size": 2,
    "dinners_per_week": 5,
    "cook_days": ["Mon", "Tue", "Wed", "Thu", "Sun"],
    "max_prep_minutes": 60,
    "dietary": "",           # e.g. "no pork, pescatarian on Fridays"
    "dislikes": "",          # comma-separated ingredients
    "favorite_cuisines": "", # comma-separated
    "notes": "",             # free-form guidance for the planner
    "new_recipe_ratio": 0.4, # share of AI-planned meals that should be brand new
    "use_ai": True,
    "always_buy": "",
    "day_themes": {},        # {"Tue": "tacos", "Mon": "meatless", "Wed": "quick"}        # newline-separated grocery lines added to every order
}

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def db_path() -> str:
    return os.environ.get(
        "RECIPE_DB", str(Path(__file__).resolve().parent.parent / "instance" / "planner.db")
    )


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(plan_meals)")}
    if "locked" not in cols:
        conn.execute("ALTER TABLE plan_meals ADD COLUMN locked INTEGER DEFAULT 0")
    if "leftovers" not in cols:
        conn.execute("ALTER TABLE plan_meals ADD COLUMN leftovers INTEGER DEFAULT 0")


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    if conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 0:
        from .seed_recipes import SEED_RECIPES

        for r in SEED_RECIPES:
            add_recipe(conn, r, source="library")
    if conn.execute("SELECT COUNT(*) FROM pantry").fetchone()[0] == 0:
        for staple in ["salt", "black pepper", "olive oil", "water", "vegetable oil"]:
            conn.execute("INSERT OR IGNORE INTO pantry(name) VALUES (?)", (staple,))
    conn.commit()


# ---------- settings ----------

def get_settings(conn) -> dict:
    out = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        out[row["key"]] = json.loads(row["value"])
    return out


def save_settings(conn, values: dict) -> None:
    for k, v in values.items():
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (k, json.dumps(v)),
        )
    conn.commit()


# ---------- recipes ----------

def _recipe_row(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["ingredients"] = json.loads(d["ingredients"])
    d["steps"] = json.loads(d["steps"])
    d["tags"] = json.loads(d["tags"] or "[]")
    return d


def add_recipe(conn, r: dict, source: str = "manual") -> int:
    cur = conn.execute(
        "INSERT INTO recipes(title, description, cuisine, protein, servings, prep_minutes,"
        " ingredients, steps, tags, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            r["title"].strip(),
            r.get("description", ""),
            r.get("cuisine", ""),
            r.get("protein", ""),
            int(r.get("servings") or 2),
            int(r.get("prep_minutes") or 30),
            json.dumps(r.get("ingredients", [])),
            json.dumps(r.get("steps", [])),
            json.dumps(r.get("tags", [])),
            source,
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_recipe(conn, recipe_id: int, r: dict) -> None:
    conn.execute(
        "UPDATE recipes SET title=?, description=?, cuisine=?, protein=?, servings=?,"
        " prep_minutes=?, ingredients=?, steps=?, tags=? WHERE id=?",
        (
            r["title"].strip(),
            r.get("description", ""),
            r.get("cuisine", ""),
            r.get("protein", ""),
            int(r.get("servings") or 2),
            int(r.get("prep_minutes") or 30),
            json.dumps(r.get("ingredients", [])),
            json.dumps(r.get("steps", [])),
            json.dumps(r.get("tags", [])),
            recipe_id,
        ),
    )
    conn.commit()


def get_recipe(conn, recipe_id: int) -> dict | None:
    return _recipe_row(conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone())


def find_recipe_by_title(conn, title: str) -> dict | None:
    return _recipe_row(
        conn.execute(
            "SELECT * FROM recipes WHERE lower(title)=lower(?) ORDER BY id DESC LIMIT 1",
            (title.strip(),),
        ).fetchone()
    )


def list_recipes(conn, include_archived: bool = False) -> list[dict]:
    sql = "SELECT * FROM recipes"
    if not include_archived:
        sql += " WHERE archived=0"
    sql += " ORDER BY title"
    return [_recipe_row(r) for r in conn.execute(sql)]


def set_archived(conn, recipe_id: int, archived: bool) -> None:
    conn.execute("UPDATE recipes SET archived=? WHERE id=?", (1 if archived else 0, recipe_id))
    conn.commit()


def recipe_stats(conn, before_week: str | None = None) -> dict[int, dict]:
    """Per-recipe feedback aggregates + last time it was planned (optionally only weeks
    before `before_week`, so re-planning a week doesn't count against itself)."""
    stats: dict[int, dict] = {}
    for row in conn.execute(
        "SELECT recipe_id, AVG(rating) AS avg, COUNT(*) AS n,"
        " SUM(make_again='no') AS nos, SUM(make_again='yes') AS yeses"
        " FROM feedback GROUP BY recipe_id"
    ):
        stats[row["recipe_id"]] = {
            "avg_rating": row["avg"],
            "ratings": row["n"],
            "no_votes": row["nos"] or 0,
            "yes_votes": row["yeses"] or 0,
        }
    for row in conn.execute(
        "SELECT pm.recipe_id, MAX(p.week_start) AS last_week, COUNT(*) AS times"
        " FROM plan_meals pm JOIN plans p ON p.id = pm.plan_id"
        " WHERE (? IS NULL OR p.week_start < ?) GROUP BY pm.recipe_id",
        (before_week, before_week),
    ):
        s = stats.setdefault(row["recipe_id"], {})
        s["last_week"] = row["last_week"]
        s["times_planned"] = row["times"]
    return stats


def taste_affinity(conn) -> dict[str, dict[str, dict]]:
    """How much you like each cuisine / protein, learned from all ratings.

    score = sum(rating - 3) / (n + 2): positive = liked, negative = disliked, and shrunk toward
    zero when there are only a few ratings so one meal doesn't swing everything.
    """
    out: dict[str, dict[str, dict]] = {"cuisine": {}, "protein": {}}
    for field in out:
        for row in conn.execute(
            f"SELECT lower(trim(r.{field})) AS k, SUM(f.rating - 3) AS total, COUNT(*) AS n,"
            " AVG(f.rating) AS avg"
            f" FROM feedback f JOIN recipes r ON r.id = f.recipe_id"
            f" WHERE trim(r.{field}) != '' GROUP BY lower(trim(r.{field}))"
        ):
            out[field][row["k"]] = {"score": row["total"] / (row["n"] + 2), "n": row["n"],
                                    "avg": row["avg"]}
    return out


# ---------- plans ----------

def create_plan(conn, week_start: str, meals: list[dict], planner: str, notes: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO plans(week_start, planner, notes) VALUES (?,?,?)",
        (week_start, planner, notes),
    )
    plan_id = cur.lastrowid
    for m in meals:
        conn.execute(
            "INSERT INTO plan_meals(plan_id, day, recipe_id, servings, leftovers) VALUES (?,?,?,?,?)",
            (plan_id, m["day"], m["recipe_id"], m["servings"], m.get("leftovers", 0)),
        )
    conn.commit()
    return plan_id


def get_plan(conn, plan_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        return None
    plan = dict(row)
    meals = []
    for m in conn.execute(
        "SELECT pm.*, f.rating AS fb_rating, f.make_again AS fb_make_again, f.notes AS fb_notes"
        " FROM plan_meals pm LEFT JOIN feedback f ON f.plan_meal_id = pm.id"
        " WHERE pm.plan_id=?",
        (plan_id,),
    ):
        meal = dict(m)
        meal["recipe"] = get_recipe(conn, m["recipe_id"])
        meal["total_servings"] = meal["servings"] + (meal.get("leftovers") or 0)
        meals.append(meal)
    meals.sort(key=lambda x: (DAYS.index(x["day"]) if x["day"] in DAYS else 99, x["id"]))
    plan["meals"] = meals
    return plan


def latest_plan(conn) -> dict | None:
    row = conn.execute("SELECT id FROM plans ORDER BY week_start DESC, id DESC LIMIT 1").fetchone()
    return get_plan(conn, row["id"]) if row else None


def list_plans(conn) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT p.*, COUNT(pm.id) AS meal_count,"
            " (SELECT COUNT(*) FROM feedback f JOIN plan_meals x ON x.id=f.plan_meal_id"
            "  WHERE x.plan_id=p.id) AS feedback_count"
            " FROM plans p LEFT JOIN plan_meals pm ON pm.plan_id=p.id"
            " GROUP BY p.id ORDER BY p.week_start DESC, p.id DESC"
        )
    ]


def delete_plan(conn, plan_id: int) -> None:
    conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
    conn.commit()


def get_meal(conn, meal_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM plan_meals WHERE id=?", (meal_id,)).fetchone()
    return dict(row) if row else None


def update_meal(conn, meal_id: int, **fields) -> None:
    allowed = {"recipe_id", "servings", "cooked", "day", "locked", "leftovers"}
    sets = [(k, v) for k, v in fields.items() if k in allowed]
    if not sets:
        return
    conn.execute(
        f"UPDATE plan_meals SET {', '.join(k + '=?' for k, _ in sets)} WHERE id=?",
        [v for _, v in sets] + [meal_id],
    )
    conn.commit()


def delete_meal(conn, meal_id: int) -> None:
    conn.execute("DELETE FROM plan_meals WHERE id=?", (meal_id,))
    conn.commit()


def add_meal(conn, plan_id: int, day: str, recipe_id: int, servings: int) -> int:
    cur = conn.execute(
        "INSERT INTO plan_meals(plan_id, day, recipe_id, servings) VALUES (?,?,?,?)",
        (plan_id, day, recipe_id, servings),
    )
    conn.commit()
    return cur.lastrowid


# ---------- feedback ----------

def save_feedback(conn, plan_meal_id: int, recipe_id: int, rating: int, make_again: str, notes: str):
    prev = conn.execute(
        "SELECT make_again FROM feedback WHERE plan_meal_id=?", (plan_meal_id,)
    ).fetchone()
    if prev and prev["make_again"] == "no" and make_again != "no":
        # Changed their mind: bring the recipe back unless another rating also said "never".
        others = conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE recipe_id=? AND make_again='no' AND plan_meal_id!=?",
            (recipe_id, plan_meal_id),
        ).fetchone()[0]
        if not others:
            conn.execute("UPDATE recipes SET archived=0 WHERE id=?", (recipe_id,))
    conn.execute("DELETE FROM feedback WHERE plan_meal_id=?", (plan_meal_id,))
    conn.execute(
        "INSERT INTO feedback(recipe_id, plan_meal_id, rating, make_again, notes) VALUES (?,?,?,?,?)",
        (recipe_id, plan_meal_id, rating, make_again, notes.strip()),
    )
    conn.execute("UPDATE plan_meals SET cooked=1 WHERE id=?", (plan_meal_id,))
    if make_again == "no":
        conn.execute("UPDATE recipes SET archived=1 WHERE id=?", (recipe_id,))
    conn.commit()


def recipe_feedback(conn, recipe_id: int) -> list[dict]:
    """All feedback for one recipe, newest first."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM feedback WHERE recipe_id=? ORDER BY created_at DESC, id DESC", (recipe_id,))]


def recent_feedback(conn, limit: int = 40) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT f.*, r.title FROM feedback f JOIN recipes r ON r.id=f.recipe_id"
            " ORDER BY f.created_at DESC, f.id DESC LIMIT ?",
            (limit,),
        )
    ]


# ---------- pantry & product picks ----------

def pantry(conn) -> list[str]:
    return [r["name"] for r in conn.execute("SELECT name FROM pantry ORDER BY name")]


def set_pantry(conn, names: list[str]) -> None:
    from .grocery import normalize_name

    conn.execute("DELETE FROM pantry")
    for n in {normalize_name(x) for x in names if x.strip()}:
        conn.execute("INSERT OR IGNORE INTO pantry(name) VALUES (?)", (n,))
    conn.commit()


def product_picks(conn) -> dict[str, dict]:
    return {r["name"]: dict(r) for r in conn.execute("SELECT * FROM product_picks")}


def set_product_pick(conn, name: str, url: str, label: str = "") -> None:
    from .grocery import normalize_name

    name = normalize_name(name)
    if not url.strip():
        conn.execute("DELETE FROM product_picks WHERE name=?", (name,))
    else:
        conn.execute(
            "INSERT INTO product_picks(name, url, label) VALUES (?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET url=excluded.url, label=excluded.label",
            (name, url.strip(), label.strip()),
        )
    conn.commit()


# ---------- orders ----------

def create_order(conn, plan_id: int | None, items: list[dict]) -> int:
    cur = conn.execute(
        "INSERT INTO orders(plan_id, items) VALUES (?, ?)", (plan_id, json.dumps(items))
    )
    conn.commit()
    return cur.lastrowid


def get_order(conn, order_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["items"] = json.loads(d["items"])
    return d


def save_order_items(conn, order_id: int, items: list[dict]) -> None:
    conn.execute("UPDATE orders SET items=? WHERE id=?", (json.dumps(items), order_id))
    conn.commit()


def mark_order_placed(conn, order_id: int) -> None:
    conn.execute(
        "UPDATE orders SET status='placed', placed_at=CURRENT_TIMESTAMP WHERE id=?", (order_id,)
    )
    conn.commit()


def order_for_plan(conn, plan_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id FROM orders WHERE plan_id=? ORDER BY id DESC LIMIT 1", (plan_id,)
    ).fetchone()
    return get_order(conn, row["id"]) if row else None


def list_orders(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM orders ORDER BY id DESC"):
        d = dict(r)
        items = json.loads(d["items"])
        d["item_count"] = len(items)
        d["added_count"] = sum(1 for i in items if i.get("added"))
        out.append(d)
    return out
