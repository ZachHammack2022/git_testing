# Meal Planner

A personal, single-user weekly dinner planner:

1. **Plans the week's dinners.** It picks from your recipe library based on your ratings. Optionally, Claude also writes new recipes for you.
2. **Orders the food from Whole Foods on Amazon.** It builds one combined shopping list and walks you through filling your Whole Foods cart.
3. **Learns from your feedback.** Star ratings, "make again / never again" and notes all change what gets planned next.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m recipe_planner          # then open http://127.0.0.1:5000
```

Your data lives in `instance/planner.db` (SQLite). Set `RECIPE_DB=/path/to/file.db` to keep it somewhere else.

### Optional: Claude planning

```bash
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python -m recipe_planner
```

With a key set, Claude plans the week using your preferences, your feedback history and your recipe library. It reuses recipes you liked and adds new ones; you set how many under Settings. You can also:

- ask for a swap ("something lighter")
- paste any recipe, or describe a dish, and have it turned into a structured recipe

If a Claude call fails, the built-in planner takes over. Set `RECIPE_PLANNER_MODEL` to use a different model.

## The weekly loop

| Step | Where | What happens |
|---|---|---|
| Plan | **This week → Make plan** | One dinner per cook day (set in Settings). You can swap, re-pick, resize or remove any meal. |
| Shop | **Shopping list** | Ingredients from every meal are combined and unit-converted (tbsp + cups, oz + lb), scaled to your servings, and grouped by aisle. Pantry staples start unchecked. Tick "remember unchecked items" to add new staples. |
| Order | **Order from Whole Foods** | Each item links to a Whole Foods search on Amazon, or straight to a product you saved for it. **Open next item** opens the next unchecked item in a new tab and ticks it off. A progress bar shows how far along you are. When you've checked out on Amazon, mark the order placed. |
| Rate | **Rate meals** | 1–5 stars, "make again / with changes / never again", and notes. "Never again" retires the recipe. General notes ("more fish") are added to your standing planner notes. |

### About "ordering"

Amazon has no public API for placing Whole Foods orders, so the app can't check out for you. What it does is make building the cart quick:

- every item is one click away
- products you save are remembered
- each order is tracked

You choose the delivery window and pay on Amazon as usual.

## How feedback changes the plan

- **Built-in planner:**
  - Liked recipes score higher. Recipes with "make again" votes get a bonus.
  - Recipes you haven't tried yet get a small bonus.
  - Recipes planned in the last 1–3 weeks score lower.
  - It avoids repeating a cuisine or protein within the same week.
  - Disliked ingredients and retired recipes are never picked.
  - Your favorite cuisines get a boost. Recipes longer than your time limit score lower.
- **Claude:** gets your preferences, your last 40 pieces of feedback (with notes), the library with average ratings and when each recipe was last planned, and the list of retired recipes.

## Development

```bash
.venv/bin/python -m pytest -q
```

Layout: `recipe_planner/`
- `db.py`: SQLite schema and queries
- `planner.py`: built-in planner and plan orchestration
- `ai.py`: Claude integration
- `grocery.py`: shopping-list aggregation and units
- `ordering.py`: Whole Foods links
- `app.py`: Flask routes
- `templates/`, `static/`: the web pages and styles
