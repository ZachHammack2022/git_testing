#!/usr/bin/env bash
# One-command start: creates the virtualenv on first run, then launches the app.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
PORT="${PORT:-5000}"
URL="http://127.0.0.1:${PORT}"
echo "Meal Planner → ${URL}"
( sleep 1.5; (command -v open >/dev/null && open "$URL") || (command -v xdg-open >/dev/null && xdg-open "$URL") || true ) >/dev/null 2>&1 &
exec .venv/bin/python -m recipe_planner --port "$PORT"
