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
| Stock | **Kitchen** | Snap a photo of your fridge, a pantry shelf or a grocery receipt and Claude lists the food it sees. You confirm each item as **Use up soon** (fresh food) or **Pantry staple**. Fresh food pre-fills the next plan's **Use up** field and starts unchecked on shopping lists; it drops off after 10 days. You can also just type what you have. |
| Prep | **🔪 Prep plan** (on the plan) | One checklist for the week's prep. Chopping is combined across dinners ("Mince garlic (5 cloves) for Mon, Tue, Wed"). Grains are cooked once, and sauces are made ahead. Meat and fish for Thursday onward get a freeze reminder. It's split into a weekend session for Mon–Wed and a Wednesday top-up for Thu–Sun, because chopped vegetables keep 3–4 days. Night-before reminders cover thawing and marinades. **Make a smarter plan with Claude** writes an ordered plan with timings. Ticks are saved. |
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
- **Bot checks:** if Amazon shows a sign-in page or an "are you human" check, the browser window comes to the front. The order page shows a pulsing "✋ Needs you" alert, the tab title changes, and you get a desktop notification if you allowed them. Click the button in the window and it continues by itself. It never clicks through these checks itself: they exist to stop automation, and getting around them could get your Amazon account flagged. It also leaves a few seconds between items, which makes checks rarer.
- **When it's done:** it reads your Amazon and Whole Foods carts, so the ticks match what's really there, then closes its window. Review the cart in your normal browser; it's the same Amazon cart. The order page shows a "🛒 Your cart is ready for review" card with a **Review cart on Amazon** button. It lists anything you need to add yourself, with links, and you get a notification.
- **Cart sync:** the order page keeps its ticks matched to your real cart. It checks with a hidden background browser that reuses the same Amazon sign-in. Checks happen when you open the page, whenever you switch back to its tab (for example after editing your cart on Amazon), and every few minutes while it's open. There's also a **↻ Sync with cart** button. Items in your cart that aren't on the list are shown too. If you empty your cart, the ticks clear. Items are matched by product, or by name when the product isn't known. If Amazon wants you to sign in first, the sync tells you instead of clearing anything. Amazon offers no way to be notified about cart changes, so this is checking, not instant push.
- **Product preview:** **🔍 Preview products** on the order page looks up each item on Whole Foods in the same hidden background browser. Each item gets a card with a photo, name, price and unit price. Flip through the top results with ‹ › and press **Use this one** to save that product. The cart filler then adds exactly that product. The order shows an estimated total for one of each item. If Amazon wants a sign-in or a check, the preview stops and offers **Preview with the browser showing** so you can deal with it.
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

## Hosting

See [docs/HOSTING.md](docs/HOSTING.md) for reaching the app from anywhere and what it would take to make it multi-user.

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
