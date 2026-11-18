"""Flask web app: plan -> shop/order -> cook -> feedback -> better plans."""
from __future__ import annotations

import datetime as dt
import hmac
import os
import secrets

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, url_for)

from . import ai, db, grocery, importer, ordering, planner


def create_app(db_file: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("RECIPE_PLANNER_SECRET") or secrets.token_hex(32)
    app.config["DB_FILE"] = db_file or db.db_path()
    app.config["PASSWORD"] = os.environ.get("RECIPE_PLANNER_PASSWORD", "")

    @app.before_request
    def _require_password():
        """Optional HTTP basic auth, for when the app is reachable from other devices."""
        pw = app.config["PASSWORD"]
        if not pw or request.endpoint in ("static", "manifest"):
            return None
        auth = request.authorization
        if auth and auth.password and hmac.compare_digest(auth.password.encode(), pw.encode()):
            return None
        return Response("Password required.", 401,
                        {"WWW-Authenticate": 'Basic realm="Meal Planner", charset="UTF-8"'})

    @app.get("/manifest.webmanifest")
    def manifest():
        return jsonify({
            "name": "Meal Planner", "short_name": "Meals", "start_url": "/", "display": "standalone",
            "background_color": "#f7f6f2", "theme_color": "#2f7d4f",
            "icons": [{"src": url_for("static", filename="icon.svg"), "sizes": "any",
                       "type": "image/svg+xml"}],
        })

    with db.connect(app.config["DB_FILE"]) as conn:
        db.init(conn)

    def get_conn():
        if "conn" not in g:
            g.conn = db.connect(app.config["DB_FILE"])
        return g.conn

    @app.teardown_appcontext
    def _close(_exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def _globals():
        return {"ai_available": ai.available(), "DAYS": db.DAYS,
                "WHOLE_FOODS": ordering.WHOLE_FOODS_STOREFRONT, "AMAZON_CART": ordering.AMAZON_CART}

    @app.template_filter("pretty_date")
    def _pretty_date(s):
        try:
            d = dt.date.fromisoformat(s[:10])
            return f"{d:%b} {d.day}, {d.year}"  # no %-d: it isn't portable to Windows
        except (TypeError, ValueError):
            return s

    @app.template_filter("ingredient_line")
    def _ingredient_line(ing):
        return grocery.ingredient_to_line(ing)

    def plan_or_404(plan_id):
        plan = db.get_plan(get_conn(), plan_id)
        if not plan:
            abort(404)
        return plan

    # ---------------- home & plans ----------------

    @app.get("/")
    def home():
        conn = get_conn()
        plan = db.latest_plan(conn)
        order = db.order_for_plan(conn, plan["id"]) if plan else None
        needs_feedback, tonight = [], None
        if plan:
            needs_feedback = [m for m in plan["meals"] if m["fb_rating"] is None]
            today = dt.date.today()
            offset = (today - dt.date.fromisoformat(plan["week_start"])).days
            if 0 <= offset < 7:
                tonight = next((m for m in plan["meals"] if m["day"] == db.DAYS[offset]), None)
        return render_template(
            "home.html", plan=plan, order=order, needs_feedback=needs_feedback, tonight=tonight,
            next_week=planner.next_unplanned_week(plan["week_start"] if plan else None).isoformat(),
            settings=db.get_settings(conn),
        )

    @app.post("/plans")
    def new_plan():
        conn = get_conn()
        week = request.form.get("week_start") or planner.upcoming_monday().isoformat()
        try:
            week_start = dt.date.fromisoformat(week)
        except ValueError:
            flash("Invalid date.", "error")
            return redirect(url_for("home"))
        week_start -= dt.timedelta(days=week_start.weekday())  # snap to Monday
        use_ai = request.form.get("use_ai") == "on"
        plan_id = planner.plan_week(conn, week_start, use_ai=use_ai,
                                    extra_request=request.form.get("request", ""),
                                    use_up=planner.split_csv(request.form.get("use_up", "")))
        plan = db.get_plan(conn, plan_id)
        if plan["notes"]:
            flash(plan["notes"], "info")
        return redirect(url_for("show_plan", plan_id=plan_id))

    @app.get("/plans/<int:plan_id>")
    def show_plan(plan_id):
        conn = get_conn()
        plan = plan_or_404(plan_id)
        used_days = {m["day"] for m in plan["meals"]}
        return render_template(
            "plan.html", plan=plan, recipes=db.list_recipes(conn),
            free_days=[d for d in db.DAYS if d not in used_days],
            order=db.order_for_plan(conn, plan_id),
        )

    @app.post("/plans/<int:plan_id>/repeat")
    def repeat_plan(plan_id):
        conn = get_conn()
        plan = plan_or_404(plan_id)
        latest = db.latest_plan(conn)
        week = planner.next_unplanned_week(latest["week_start"] if latest else None)
        meals = [{"day": m["day"], "recipe_id": m["recipe_id"], "servings": m["servings"],
                  "leftovers": m["leftovers"]}
                 for m in plan["meals"] if not m["recipe"]["archived"]]
        new_id = db.create_plan(conn, week.isoformat(), meals, plan["planner"],
                                f"Repeat of the week of {_pretty_date(plan['week_start'])}.")
        flash("Copied — swap anything you like.", "info")
        return redirect(url_for("show_plan", plan_id=new_id))

    @app.post("/plans/<int:plan_id>/delete")
    def delete_plan(plan_id):
        db.delete_plan(get_conn(), plan_id)
        flash("Plan deleted.", "info")
        return redirect(url_for("history"))

    @app.post("/plans/<int:plan_id>/meals")
    def add_meal(plan_id):
        conn = get_conn()
        plan_or_404(plan_id)
        day = request.form.get("day")
        recipe_id = request.form.get("recipe_id", type=int)
        if day not in db.DAYS or not recipe_id or not db.get_recipe(conn, recipe_id):
            flash("Pick a day and a recipe.", "error")
        else:
            servings = int(db.get_settings(conn)["household_size"])
            db.add_meal(conn, plan_id, day, recipe_id, servings)
        return redirect(url_for("show_plan", plan_id=plan_id))

    @app.post("/plans/<int:plan_id>/regenerate")
    def regenerate_plan(plan_id):
        conn = get_conn()
        plan_or_404(plan_id)
        msg = planner.regenerate(conn, plan_id, use_ai=request.form.get("use_ai") == "on",
                                 extra_request=request.form.get("request", ""),
                                 use_up=planner.split_csv(request.form.get("use_up", "")))
        flash(msg, "info")
        return redirect(url_for("show_plan", plan_id=plan_id))

    @app.post("/meals/<int:meal_id>/lock")
    def lock_meal(meal_id):
        conn = get_conn()
        meal = db.get_meal(conn, meal_id) or abort(404)
        db.update_meal(conn, meal_id, locked=0 if meal["locked"] else 1)
        if request.is_json:
            return jsonify(locked=not meal["locked"])
        return redirect(url_for("show_plan", plan_id=meal["plan_id"]) + f"#meal-{meal_id}")

    @app.get("/plans/<int:plan_id>.ics")
    def plan_ics(plan_id):
        plan = plan_or_404(plan_id)
        week = dt.date.fromisoformat(plan["week_start"])
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        def esc(t):
            return t.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//meal-planner//EN"]
        for m in plan["meals"]:
            day = week + dt.timedelta(days=db.DAYS.index(m["day"]))
            r = m["recipe"]
            desc = f"{r['prep_minutes']} min · serves {m['servings']}\n" + url_for("cook", meal_id=m["id"], _external=True)
            lines += ["BEGIN:VEVENT", f"UID:meal-{m['id']}@meal-planner", f"DTSTAMP:{stamp}",
                      f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
                      f"DTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}",
                      f"SUMMARY:{esc('Dinner: ' + r['title'])}", f"DESCRIPTION:{esc(desc)}", "END:VEVENT"]
        lines.append("END:VCALENDAR")
        return Response("\r\n".join(lines) + "\r\n", mimetype="text/calendar",
                        headers={"Content-Disposition": f"attachment; filename=dinners-{plan['week_start']}.ics"})

    @app.post("/meals/<int:meal_id>/leftovers")
    def toggle_leftovers(meal_id):
        conn = get_conn()
        meal = db.get_meal(conn, meal_id) or abort(404)
        extra = 0 if meal["leftovers"] else int(db.get_settings(conn)["household_size"])
        db.update_meal(conn, meal_id, leftovers=extra)
        return redirect(url_for("show_plan", plan_id=meal["plan_id"]) + f"#meal-{meal_id}")

    @app.post("/meals/<int:meal_id>/swap")
    def swap_meal(meal_id):
        conn = get_conn()
        meal = db.get_meal(conn, meal_id) or abort(404)
        use_ai = request.form.get("use_ai") == "on"
        msg = planner.swap_meal(conn, meal_id, request.form.get("request", ""), use_ai=use_ai)
        flash(msg, "info")
        return redirect(url_for("show_plan", plan_id=meal["plan_id"]) + f"#meal-{meal_id}")

    @app.post("/meals/<int:meal_id>/update")
    def update_meal(meal_id):
        conn = get_conn()
        meal = db.get_meal(conn, meal_id) or abort(404)
        fields = {}
        if request.form.get("servings"):
            fields["servings"] = max(1, min(20, request.form.get("servings", type=int) or 1))
        if request.form.get("day") in db.DAYS:
            fields["day"] = request.form["day"]
        rid = request.form.get("recipe_id", type=int)
        if rid and db.get_recipe(conn, rid):
            fields["recipe_id"] = rid
        db.update_meal(conn, meal_id, **fields)
        return redirect(url_for("show_plan", plan_id=meal["plan_id"]) + f"#meal-{meal_id}")

    @app.post("/meals/<int:meal_id>/delete")
    def delete_meal(meal_id):
        conn = get_conn()
        meal = db.get_meal(conn, meal_id) or abort(404)
        db.delete_meal(conn, meal_id)
        return redirect(url_for("show_plan", plan_id=meal["plan_id"]))

    # ---------------- shopping & ordering ----------------

    def _shopping(plan):
        return grocery.build_shopping_list(plan["meals"], db.pantry(get_conn()))

    @app.get("/plans/<int:plan_id>/shopping")
    def shopping(plan_id):
        plan = plan_or_404(plan_id)
        items = _shopping(plan)
        return render_template(
            "shopping.html", plan=plan, groups=grocery.group_by_category(items),
            always_buy=[l for l in db.get_settings(get_conn()).get("always_buy", "").splitlines() if l.strip()],
            text=grocery.as_text(items), order=db.order_for_plan(get_conn(), plan_id),
        )

    @app.post("/plans/<int:plan_id>/order")
    def start_order(plan_id):
        conn = get_conn()
        plan = plan_or_404(plan_id)
        items = _shopping(plan)
        if "buy" in request.form:  # explicit selection from the shopping page
            buy = set(request.form.getlist("buy"))
            if request.form.get("remember_pantry"):
                skipped = [i["key"] for i in items if i["key"] not in buy]
                db.set_pantry(conn, db.pantry(conn) + skipped)
            items = [i for i in items if i["key"] in buy]
            for it in items:
                it["in_pantry"] = False
        picks = db.product_picks(conn)
        rows = ordering.order_items(items, picks)
        if "include_always" not in request.form or "on" in request.form.getlist("include_always"):
            always = db.get_settings(conn).get("always_buy", "").splitlines()
            rows = ordering.merge_extras(rows, always, picks, "always buy")
        order_id = db.create_order(conn, plan_id, rows)
        return redirect(url_for("show_order", order_id=order_id))

    @app.get("/orders/<int:order_id>")
    def show_order(order_id):
        conn = get_conn()
        order = db.get_order(conn, order_id) or abort(404)
        groups = grocery.group_by_category(order["items"])
        plan = db.get_plan(conn, order["plan_id"]) if order["plan_id"] else None
        return render_template("order.html", order=order, groups=groups, plan=plan,
                               text=grocery.as_text(order["items"]))

    @app.post("/orders/<int:order_id>/items/<path:key>")
    def toggle_order_item(order_id, key):
        conn = get_conn()
        order = db.get_order(conn, order_id) or abort(404)
        added = (request.get_json(silent=True) or {}).get("added")
        if added is None:
            added = request.form.get("added") == "1"
        for it in order["items"]:
            if it["key"] == key:
                it["added"] = bool(added)
        db.save_order_items(conn, order_id, order["items"])
        done = sum(1 for i in order["items"] if i["added"])
        if request.is_json:
            return jsonify(ok=True, added=done, total=len(order["items"]))
        return redirect(url_for("show_order", order_id=order_id))

    @app.post("/orders/<int:order_id>/add")
    def add_order_item(order_id):
        conn = get_conn()
        order = db.get_order(conn, order_id) or abort(404)
        lines = request.form.get("items", "").replace(",", "\n").splitlines()
        items = ordering.merge_extras(order["items"], lines, db.product_picks(conn), "added")
        db.save_order_items(conn, order_id, items)
        return redirect(url_for("show_order", order_id=order_id))

    @app.post("/orders/<int:order_id>/items/<path:key>/remove")
    def remove_order_item(order_id, key):
        conn = get_conn()
        order = db.get_order(conn, order_id) or abort(404)
        db.save_order_items(conn, order_id, [i for i in order["items"] if i["key"] != key])
        return redirect(url_for("show_order", order_id=order_id))

    @app.post("/orders/<int:order_id>/placed")
    def order_placed(order_id):
        conn = get_conn()
        order = db.get_order(conn, order_id) or abort(404)
        raw = request.form.get("total", "").replace("$", "").replace(",", "").strip()
        try:
            total = round(float(raw), 2) if raw else None
        except ValueError:
            flash("Total should be a number, like 142.37.", "error")
            return redirect(url_for("show_order", order_id=order_id))
        db.mark_order_placed(conn, order_id, total)
        if order["status"] != "placed":
            flash("Order marked as placed. Enjoy the week — come back to rate the meals!", "info")
        else:
            flash("Saved.", "info")
        return redirect(url_for("show_order", order_id=order_id))

    @app.post("/orders/<int:order_id>/picks")
    def save_pick(order_id):
        conn = get_conn()
        order = db.get_order(conn, order_id) or abort(404)
        key = request.form.get("key", "")
        url = request.form.get("url", "").strip()
        if url and not url.startswith(("https://www.amazon.com/", "https://amazon.com/", "https://a.co/")):
            flash("Product links should be amazon.com links.", "error")
        else:
            db.set_product_pick(conn, key, url, request.form.get("label", ""))
            db.save_order_items(conn, order_id, ordering.refresh_links(order["items"], db.product_picks(conn)))
            flash("Saved — future orders will link straight to this product." if url else "Product link cleared.", "info")
        return redirect(url_for("show_order", order_id=order_id) + f"#item-{key.replace(' ', '-')}")

    @app.get("/meals/<int:meal_id>/cook")
    def cook(meal_id):
        conn = get_conn()
        meal = db.get_meal(conn, meal_id) or abort(404)
        plan = db.get_plan(conn, meal["plan_id"])
        m = next(x for x in plan["meals"] if x["id"] == meal_id)
        r = m["recipe"]
        scale = m["total_servings"] / max(int(r["servings"] or 1), 1)
        ingredients = [dict(i, quantity=grocery.parse_quantity(i.get("quantity")) * scale)
                       for i in r["ingredients"]]
        past_notes = [f for f in db.recipe_feedback(conn, r["id"])
                      if f["notes"] and f["plan_meal_id"] != meal_id][:5]
        return render_template("cook.html", m=m, r=r, plan=plan, ingredients=ingredients,
                               scaled=abs(scale - 1) > 0.01, past_notes=past_notes)

    # ---------------- feedback ----------------

    @app.post("/meals/<int:meal_id>/feedback")
    def save_meal_feedback(meal_id):
        conn = get_conn()
        meal = db.get_meal(conn, meal_id) or abort(404)
        rating = request.form.get("rating", type=int)
        if not rating:
            flash("Pick a star rating.", "error")
            return redirect(url_for("cook", meal_id=meal_id))
        make_again = request.form.get("again", "yes")
        if make_again not in ("yes", "tweak", "no"):
            make_again = "yes"
        db.save_feedback(conn, meal_id, meal["recipe_id"], max(1, min(5, rating)), make_again,
                         request.form.get("notes", ""))
        flash("Thanks! That'll shape future plans.", "info")
        return redirect(url_for("home"))

    @app.get("/plans/<int:plan_id>/feedback")
    def feedback(plan_id):
        return render_template("feedback.html", plan=plan_or_404(plan_id))

    @app.post("/plans/<int:plan_id>/feedback")
    def save_feedback(plan_id):
        conn = get_conn()
        plan = plan_or_404(plan_id)
        saved = 0
        for m in plan["meals"]:
            rating = request.form.get(f"rating-{m['id']}", type=int)
            if not rating:
                continue
            make_again = request.form.get(f"again-{m['id']}", "yes")
            if make_again not in ("yes", "tweak", "no"):
                make_again = "yes"
            db.save_feedback(conn, m["id"], m["recipe_id"], max(1, min(5, rating)), make_again,
                             request.form.get(f"notes-{m['id']}", ""))
            saved += 1
        general = request.form.get("general_notes", "").strip()
        if general:
            settings = db.get_settings(conn)
            stamp = dt.date.today().isoformat()
            notes = (settings.get("notes", "") + f"\n[{stamp}] {general}").strip()
            db.save_settings(conn, {"notes": notes})
        flash(f"Saved feedback for {saved} meal(s). Next week's plan will take it into account.", "info")
        return redirect(url_for("home"))

    # ---------------- recipes ----------------

    @app.get("/recipes")
    def recipes():
        conn = get_conn()
        q = request.args.get("q", "").strip().lower()
        show_archived = request.args.get("archived") == "1"
        items = db.list_recipes(conn, include_archived=show_archived)
        if show_archived:
            items = [r for r in items if r["archived"]]
        if q:
            items = [r for r in items if q in r["title"].lower() or q in r["cuisine"].lower()
                     or q in r["protein"].lower() or any(q in t for t in r["tags"])]
        stats = db.recipe_stats(conn)
        sort = request.args.get("sort", "name")
        if request.args.get("untried") == "1":
            items = [r for r in items if not stats.get(r["id"], {}).get("ratings")]
        if sort == "rating":
            items.sort(key=lambda r: -(stats.get(r["id"], {}).get("avg_rating") or 0))
        elif sort == "quick":
            items.sort(key=lambda r: r["prep_minutes"] or 999)
        elif sort == "newest":
            items.sort(key=lambda r: -r["id"])
        return render_template("recipes.html", recipes=items, stats=stats, q=q,
                               show_archived=show_archived, sort=sort,
                               untried=request.args.get("untried") == "1")

    @app.get("/recipes/<int:recipe_id>")
    def recipe(recipe_id):
        conn = get_conn()
        r = db.get_recipe(conn, recipe_id) or abort(404)
        fb = db.recipe_feedback(conn, recipe_id)
        return render_template("recipe.html", r=r, feedback=fb,
                               has_notes=any(f["notes"] for f in fb),
                               stats=db.recipe_stats(conn).get(recipe_id, {}),
                               latest=db.latest_plan(conn))

    def _recipe_from_form(form) -> dict:
        ingredients = [x for x in (grocery.parse_ingredient_line(l)
                                   for l in form.get("ingredients", "").splitlines()) if x]
        steps = [s.strip() for s in form.get("steps", "").splitlines() if s.strip()]
        return {
            "title": form.get("title", "").strip(),
            "description": form.get("description", "").strip(),
            "cuisine": form.get("cuisine", "").strip(),
            "protein": form.get("protein", "").strip().lower(),
            "servings": form.get("servings", type=int) or 4,
            "prep_minutes": form.get("prep_minutes", type=int) or 30,
            "ingredients": ingredients,
            "steps": steps,
            "tags": [t.strip().lower() for t in form.get("tags", "").split(",") if t.strip()],
        }

    @app.route("/recipes/new", methods=["GET", "POST"])
    def new_recipe():
        conn = get_conn()
        if request.method == "POST":
            r = _recipe_from_form(request.form)
            if not r["title"] or not r["ingredients"]:
                flash("A recipe needs a title and at least one ingredient.", "error")
                return render_template("recipe_form.html", r=r, action=url_for("new_recipe"))
            rid = db.add_recipe(conn, r, source="manual")
            return redirect(url_for("recipe", recipe_id=rid))
        return render_template("recipe_form.html", r=None, action=url_for("new_recipe"))

    @app.post("/recipes/import")
    def import_recipe():
        conn = get_conn()
        text = request.form.get("text", "").strip()
        if not text:
            flash("Paste a recipe or describe a dish.", "error")
            return redirect(url_for("new_recipe"))
        if not ai.available():
            flash("Importing needs an Anthropic API key. Use the form instead.", "error")
            return redirect(url_for("new_recipe"))
        try:
            r = ai.recipe_from_text(text, int(db.get_settings(conn)["household_size"]))
        except ai.AIError as e:
            flash(f"Import failed: {e}", "error")
            return redirect(url_for("new_recipe"))
        # Show the form pre-filled so it can be reviewed before saving.
        return render_template("recipe_form.html", r=r, action=url_for("new_recipe"))

    @app.post("/recipes/import-url")
    def import_url():
        conn = get_conn()
        url = request.form.get("url", "").strip()
        try:
            page = importer.fetch(url)
        except importer.RecipeImportError as e:
            flash(str(e), "error")
            return redirect(url_for("new_recipe"))
        r = importer.parse_jsonld(page)
        if r is None and ai.available():
            try:
                r = ai.recipe_from_text(importer.page_text(page),
                                        int(db.get_settings(conn)["household_size"]))
            except ai.AIError as e:
                flash(f"Couldn't read a recipe from that page: {e}", "error")
                return redirect(url_for("new_recipe"))
        if r is None:
            flash("No recipe data found on that page. Paste it into the form instead.", "error")
            return redirect(url_for("new_recipe"))
        flash("Imported — review and save.", "info")
        return render_template("recipe_form.html", r=r, action=url_for("new_recipe"))

    @app.post("/recipes/<int:recipe_id>/apply-notes")
    def apply_notes(recipe_id):
        conn = get_conn()
        r = db.get_recipe(conn, recipe_id) or abort(404)
        notes = [f["notes"] for f in db.recipe_feedback(conn, recipe_id) if f["notes"]]
        if not notes or not ai.available():
            flash("Needs Claude and at least one note on this recipe.", "error")
            return redirect(url_for("recipe", recipe_id=recipe_id))
        try:
            revised = ai.revise_recipe(r, notes)
        except ai.AIError as e:
            flash(f"Couldn't revise: {e}", "error")
            return redirect(url_for("recipe", recipe_id=recipe_id))
        flash("Here's the recipe with your notes worked in. Review and save, or go back to keep the original.", "info")
        return render_template("recipe_form.html", r=dict(revised, id=recipe_id),
                               action=url_for("edit_recipe", recipe_id=recipe_id))

    @app.route("/recipes/<int:recipe_id>/edit", methods=["GET", "POST"])
    def edit_recipe(recipe_id):
        conn = get_conn()
        r = db.get_recipe(conn, recipe_id) or abort(404)
        if request.method == "POST":
            new = _recipe_from_form(request.form)
            if not new["title"] or not new["ingredients"]:
                flash("A recipe needs a title and at least one ingredient.", "error")
            else:
                db.update_recipe(conn, recipe_id, new)
                return redirect(url_for("recipe", recipe_id=recipe_id))
        return render_template("recipe_form.html", r=r,
                               action=url_for("edit_recipe", recipe_id=recipe_id))

    @app.post("/recipes/<int:recipe_id>/archive")
    def archive_recipe(recipe_id):
        conn = get_conn()
        r = db.get_recipe(conn, recipe_id) or abort(404)
        db.set_archived(conn, recipe_id, not r["archived"])
        flash("Recipe retired — it won't be planned again." if not r["archived"]
              else "Recipe is back in rotation.", "info")
        return redirect(url_for("recipe", recipe_id=recipe_id))

    # ---------------- settings & history ----------------

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        conn = get_conn()
        if request.method == "POST":
            f = request.form
            days = [d for d in db.DAYS if f.get(f"day-{d}")]
            values = {
                "household_size": max(1, f.get("household_size", type=int) or 2),
                "cook_days": days or db.DAYS[:5],
                "day_themes": {d: f.get(f"theme-{d}", "").strip() for d in db.DAYS
                               if f.get(f"theme-{d}", "").strip()},
                "dinners_per_week": len(days or db.DAYS[:5]),
                "max_prep_minutes": f.get("max_prep_minutes", type=int) or 60,
                "dietary": f.get("dietary", "").strip(),
                "dislikes": f.get("dislikes", "").strip(),
                "favorite_cuisines": f.get("favorite_cuisines", "").strip(),
                "notes": f.get("notes", "").strip(),
                "new_recipe_ratio": max(0.0, min(1.0, (f.get("new_recipe_pct", type=int) or 0) / 100)),
                "use_ai": f.get("use_ai") == "on",
                "always_buy": "\n".join(l.strip() for l in f.get("always_buy", "").splitlines() if l.strip()),
            }
            db.save_settings(conn, values)
            db.set_pantry(conn, [x for x in f.get("pantry", "").replace("\n", ",").split(",")])
            flash("Settings saved.", "info")
            return redirect(url_for("settings"))
        return render_template("settings.html", s=db.get_settings(conn), pantry=db.pantry(conn),
                               picks=db.product_picks(conn))

    @app.get("/backup.db")
    def backup():
        import io
        import sqlite3

        src = get_conn()
        dst = sqlite3.connect(":memory:")
        src.backup(dst)
        data = dst.serialize()
        dst.close()
        name = f"meal-planner-backup-{dt.date.today().isoformat()}.db"
        return send_file(io.BytesIO(data), mimetype="application/x-sqlite3", as_attachment=True,
                         download_name=name)

    @app.post("/picks/delete")
    def delete_pick():
        db.set_product_pick(get_conn(), request.form.get("key", ""), "")
        return redirect(url_for("settings") + "#picks")

    @app.get("/history")
    def history():
        conn = get_conn()
        stats = db.recipe_stats(conn)
        recipes = {r["id"]: r for r in db.list_recipes(conn, include_archived=True)}
        favorites = sorted(
            ((recipes[rid], s) for rid, s in stats.items()
             if rid in recipes and s.get("avg_rating") and s["avg_rating"] >= 4 and not recipes[rid]["archived"]),
            key=lambda x: (-x[1]["avg_rating"], -x[1]["ratings"]))[:8]
        aff = db.taste_affinity(conn)
        learned = {f: sorted(aff[f].items(), key=lambda kv: -kv[1]["score"]) for f in aff}
        retired = [r for r in recipes.values() if r["archived"]]
        return render_template("history.html", plans=db.list_plans(conn), orders=db.list_orders(conn),
                               feedback=db.recent_feedback(conn, 30), favorites=favorites,
                               learned=learned, retired=retired,
                               spending=db.spending_summary(conn))

    return app
