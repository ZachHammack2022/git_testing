# Meal Planner

A personal, single-user weekly dinner planner:

1. **Plans the week's dinners.** It picks from your recipe library based on your ratings. Optionally, Claude also writes new recipes for you.
2. **Orders the food from Whole Foods on Amazon.** It builds one combined shopping list and walks you through filling your Whole Foods cart.
3. **Learns from your feedback.** Star ratings, "make again / never again" and notes all change what gets planned next.

## Run it

```bash
./run.sh            # first run sets everything up, then opens http://127.0.0.1:5055
```

Or by hand:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m recipe_planner          # then open http://127.0.0.1:5055
```

### Use it from your phone

```bash
RECIPE_PLANNER_PASSWORD=pick-something ./run.sh --lan
```

This prints an address like `http://192.168.1.20:5055`. Open it on your phone while it's on the same Wi-Fi, and use "Add to Home Screen" to get an app icon. The password is optional, but without one anyone on your network can open the app. Your browser will ask for a username and the password; the username can be anything.

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
| Plan | **This week → Make plan** | One dinner per cook day (set in Settings). Each day can have a theme ("tacos", "meatless", "quick", "fish", "pizza") that both planners follow. Mark a dinner 🥡 to cook extra for lunch the next day; the shopping list scales up to match. The optional **Use up** field favors recipes that use ingredients you already have. You can lock 🔒 meals you want to keep and re-plan the rest, or swap, re-pick, resize or remove any meal. **Add to calendar** downloads the week as an `.ics` file. |
| Shop | **Shopping list** | Ingredients from every meal are combined and unit-converted (tbsp + cups, oz + lb), scaled to your servings, rounded up to whole items, and grouped by aisle. Pantry staples start unchecked. Tick "remember unchecked items" to add new staples. Your **Always buy** items (milk, coffee…) are added automatically. |
| Order | **Order from Whole Foods** | Each item links to a Whole Foods search on Amazon, or straight to a product you saved for it. **Open next item** opens the next unchecked item in a new tab and ticks it off. A progress bar shows how far along you are. You can add one-off items or remove items. When you've checked out on Amazon, mark the order placed and, optionally, enter the total. **History** then shows your average spend per week and per dinner. |
| Cook | **Cook** (on each dinner) | Ingredients scaled to that night's servings and steps with checkboxes. Your notes from previous times you made it appear at the top. The screen stays awake while it's open. Rate the meal right at the bottom. |
| Rate | **Rate meals** | 1–5 stars, "make again / with changes / never again", and notes. "Never again" retires the recipe. General notes ("more fish") are added to your standing planner notes. |

### Adding recipes

The app starts with 41 dinners. You can sort the Recipes page by rating or cooking time, or show only recipes you haven't tried. To add more:
- **Import from a link.** This reads the structured recipe data most recipe sites include. If a page doesn't have that data and Claude is set up, Claude reads the page instead.
- **Quick add with Claude.** Paste recipe text or describe a dish.
- **Discover (Claude).** Get four new ideas based on what you rate well, and keep the ones you like.
- **Apply my notes (Claude).** On a recipe page, Claude rewrites the recipe to include your feedback notes, such as "double the garlic". You review it before saving.
- **Type it in.** One ingredient per line, e.g. `1 1/2 lb chicken thighs`.

### About "ordering"

Amazon has no public API for placing Whole Foods orders, so the app can't check out for you. What it does is make building the cart quick:

- every item is one click away
- products you save are remembered
- each order is tracked

You choose the delivery window and pay on Amazon as usual.

### Fill my cart automatically

On the order page, **Fill my cart** opens a browser window and adds each item to your Amazon cart. The order page ticks items off as it goes. When it's done, the window shows your cart so you can review it and change anything you want before checking out.

One-time setup:

```bash
.venv/bin/pip install playwright && .venv/bin/python -m playwright install chromium
```

How it works:

- **Sign-in:** the first time, it waits while you sign in to Amazon in that window. The browser profile is kept in `instance/browser-profile`, so you stay signed in after that.
- **Product choice:** for items with a saved product, it goes straight to that product. Otherwise it searches Whole Foods and takes the first result that isn't sponsored.
- **It learns:** whatever it picks is saved as that ingredient's product, so next week it skips the search. If it picked wrong, change the product on the order page once.
- **Whole Foods offer:** product links include Amazon's Whole Foods store parameter. Without it, a product page can show a different seller (for example Amazon Now), whose "Add to cart" goes to a different cart.
- **Bot checks:** if Amazon shows a sign-in page or an "are you human" check, it stops and waits for you to deal with it in the window. It never clicks through Amazon's checks itself.
- **Quantities:** it adds one of each item, so adjust quantities in the cart.
- **It never checks out.**
- **Limits:** it depends on Amazon's page layout. Anything it couldn't add is listed so you can add it by hand.

You can also run it from a terminal with `python -m recipe_planner.autocart`.

## How feedback changes the plan

- **Built-in planner:**
  - Liked recipes score higher. Recipes with "make again" votes get a bonus.
  - It learns which cuisines and proteins you rate highly, so untried recipes in those get a lift too.
  - It prefers recipes that share fresh produce with the rest of the week, so less goes to waste.
  - Recipes you haven't tried yet get a small bonus.
  - Recipes planned in the last 1–3 weeks score lower.
  - It avoids repeating a cuisine or protein within the same week.
  - Disliked ingredients and retired recipes are never picked.
  - Your favorite cuisines get a boost. Recipes longer than your time limit score lower.
- **Claude:** gets your preferences, your last 40 pieces of feedback (with notes), your average rating by cuisine and by protein, the library with average ratings and when each recipe was last planned, and the list of retired recipes. Your never-use ingredients are also checked in code, so a recipe containing one is dropped even if Claude suggests it.

**History → What the planner has learned** shows your favorites, how you rate each cuisine and protein, and your retired recipes.

## Backup

**Settings → Download backup** saves a copy of the whole database. To restore it, point `RECIPE_DB` at the downloaded file.

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
- `importer.py`: recipe import from a link
- `app.py`: Flask routes
- `templates/`, `static/`: the web pages and styles
