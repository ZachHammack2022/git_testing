#!/usr/bin/env bash
# One-command start: creates the virtualenv on first run, then launches the app and opens it.
# Extra arguments pass through, e.g. ./run.sh --lan   or   ./run.sh --port 8080
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m recipe_planner --open "$@"
