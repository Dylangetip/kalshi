#!/usr/bin/env bash
# One-command boot for the Bets stack.
#
#   ./start.sh
#
# Idempotent: creates .venv, installs deps, seeds backend/.env from the
# template if missing, then runs the FastAPI server which serves both
# /api/* and the prototype UI on the same port.

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
DEPS_MARKER="$VENV/.deps-installed"
ENV_FILE="backend/.env"
PORT="${PORT:-8000}"

# 1. virtualenv
if [ ! -d "$VENV" ]; then
    echo "[bets] creating $VENV ..."
    python3 -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

# 2. deps — reinstall when requirements.txt is newer than the marker
if [ ! -f "$DEPS_MARKER" ] || [ "backend/requirements.txt" -nt "$DEPS_MARKER" ]; then
    echo "[bets] installing dependencies ..."
    pip install -q -r backend/requirements.txt
    touch "$DEPS_MARKER"
fi

# 3. .env — copy the template on first run, prompt the user to edit it
if [ ! -f "$ENV_FILE" ]; then
    echo "[bets] no $ENV_FILE found — copying from template"
    cp backend/.env.example "$ENV_FILE"
    echo "[bets] edit $ENV_FILE later to add your Kalshi creds (optional —"
    echo "[bets] the rest of the stack works without them)"
fi

# 4. run
echo
echo "  ┌──────────────────────────────────────────────────────┐"
echo "  │  Bets — Kalshi weather trading terminal              │"
echo "  │                                                      │"
echo "  │  open:  http://localhost:$PORT/Bets.html               │"
echo "  │  api:   http://localhost:$PORT/api/health              │"
echo "  │                                                      │"
echo "  │  press Ctrl+C to stop                                │"
echo "  └──────────────────────────────────────────────────────┘"
echo
exec python -m uvicorn backend.main:app --reload --port "$PORT"
