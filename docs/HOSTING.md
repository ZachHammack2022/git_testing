# Hosting and scaling

## What the app is today

- **One process.** Flask serves the pages. Data is one SQLite file (`instance/planner.db`). Claude calls run inside the web request.
- **One person.** There are no accounts: settings, pantry, product picks and feedback are global. The optional `RECIPE_PLANNER_PASSWORD` is a single shared password.
- **Amazon automation runs on the same computer.** Fill my cart, cart sync and product preview each start a Playwright subprocess. It uses a Chrome profile in `instance/browser-profile` where *you* signed in to Amazon. Progress is passed through JSON files in `instance/`. When Amazon asks "are you human?", a window on your screen comes to the front and you click it.

That last point decides almost everything below.

## Option 1: keep it on your computer, reach it from anywhere (recommended for you)

Run it at home as now and put [Tailscale](https://tailscale.com) (free for personal use) on the computer and your phone. Then open `http://<computer-name>:5055` from anywhere: no ports opened to the internet, and nothing about the app changes. `./run.sh --lan` plus `RECIPE_PLANNER_PASSWORD` already covers phone use at home.

- **Works:** everything, including the cart filler, because the browser is on your computer next to you.
- **Doesn't work:** using the app while that computer is asleep or off.
- **Cost:** $0. **Effort:** about 10 minutes.

## Option 2: host the web app, keep a small "cart agent" at home

Put the web app on a small server, such as Fly.io, Railway or Render, or a $5-class VPS, with a persistent disk for the SQLite file. Run it with gunicorn behind HTTPS and turn on the password. Planning, recipes, shopping lists, the kitchen, prep plans and feedback then work from anywhere, even with your computer off.

The Amazon parts should **not** move to the server:

- **Sign-in:** Amazon would see a sign-in from a data-centre IP address. That brings more checks, one-time codes and possibly a locked account.
- **Bot checks:** nobody is in front of the server's browser to click "I'm human". We don't automate those clicks, and shouldn't.
- **Credentials:** a server would be holding a live Amazon session that can spend money.

Instead, a small agent on your computer polls the hosted app for jobs (fill, sync, preview), runs the Playwright code that already exists, and posts results back.

- **Changes needed:**
  - Replace the subprocess and JSON status files with a `jobs` table plus three or four API endpoints.
  - Add an agent loop (`python -m recipe_planner.agent --server https://...`) that authenticates with a token.
- **Effort:** about 1–2 days.

## Option 3: make it a generic app other people sign up for

As written, this doesn't scale past one household. It isn't slow; it simply assumes one user and one machine everywhere. Roughly, here is what changes:

| Area | Today | For many users | Effort |
|---|---|---|---|
| Accounts | none; global settings | users table, `user_id` on every table, email or Google login, per-user settings/pantry/picks | 2–3 days |
| Database | SQLite file | Postgres (SQLite is fine for one server with modest traffic; Postgres once there's more than one app server) | ½ day (plain SQL, no ORM) |
| Claude calls | inside the request (planning takes 20–60 s) | background job queue (e.g. RQ, or a jobs table), page polls for the result; per-user rate limits | 1 day |
| Claude cost | yours | Every plan sends the taste profile and library (thousands of tokens) and gets back full recipes. That costs real money per user per week, so use a cheaper model for routine calls, cache the library part of the prompt, and charge or cap. Check current API pricing before settling this. | ongoing |
| Recipes | one library | shared catalogue plus per-user ratings; the planner math already works per user | 1 day |
| Amazon cart, sync, preview | Playwright on the user's own computer | **Not something a hosted service can do for other people** (see below) | — |

### Amazon is the hard part

Running other people's Amazon accounts in *your* servers' browsers means holding their sessions: a security liability and almost certainly against Amazon's terms for automated access. It would also be blocked quickly, since it's many accounts from data-centre IPs. The realistic ways to give many users the same "fill my cart" convenience:

1. **A browser extension.** The same add-to-cart, cart-reading and preview logic runs inside the user's own Chrome, on their own Amazon tab and sign-in. The hosted app only sends the list. It's the normal pattern for shopping helpers, and checks are handled by the person who's already there. Effort: 1–2 weeks, plus store review.
2. **Stores with official APIs.** Kroger's public API can add items to a signed-in customer's cart. Instacart's Developer Platform turns a list into a shoppable page across many chains. Both are designed for exactly this and scale without browser automation. Whole Foods has no equivalent.

## Summary

- **Just for you:** Option 1 now; Option 2 if you want it available with your computer off.
- **For other people:** accounts + Postgres + a job queue is a few days. The real redesign is shopping: a browser extension for Amazon, and API integrations (Kroger, Instacart) for everything else.
