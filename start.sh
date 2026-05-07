#!/usr/bin/env bash
# One-command boot for the Bets stack.
#
#   ./start.sh
#
# Idempotent: creates .venv, installs deps, seeds backend/.env from the
# template if missing, then launches both servers in this terminal:
#   - FastAPI  on port 8000 (serves /api/* and the UI)         [api]
#   - MCP      on port 5000 (Claude Desktop / Code tools)      [mcp]
#
# The MCP server is auto-started when EITHER:
#   - .mcp-token exists in the project root, OR
#   - BETS_MCP_TOKEN is exported, OR
#   - BETS_MCP_ALLOW_NO_AUTH=1 is set (insecure; localhost-only dev)
# Skip the MCP launch entirely with BETS_DISABLE_MCP=1.
#
# Ctrl+C stops both processes cleanly.

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
DEPS_MARKER="$VENV/.deps-installed"
ENV_FILE="backend/.env"
PORT="${PORT:-8000}"
MCP_PORT="${BETS_MCP_PORT:-5000}"

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

# 4. MCP token discovery (file → env → opt-out)
if [ -z "${BETS_MCP_TOKEN:-}" ] && [ -f .mcp-token ]; then
    BETS_MCP_TOKEN="$(tr -d '[:space:]' < .mcp-token)"
    export BETS_MCP_TOKEN
fi

START_MCP=1
if [ "${BETS_DISABLE_MCP:-0}" = "1" ]; then
    START_MCP=0
elif [ -z "${BETS_MCP_TOKEN:-}" ] && [ "${BETS_MCP_ALLOW_NO_AUTH:-0}" != "1" ]; then
    START_MCP=0
fi

# 5. Banner
echo
echo "  ┌──────────────────────────────────────────────────────┐"
echo "  │  Bets — Kalshi weather trading terminal              │"
echo "  │                                                      │"
echo "  │  open:  http://localhost:$PORT/Bets.html"
echo "  │  api:   http://localhost:$PORT/api/health"
if [ "$START_MCP" = "1" ]; then
echo "  │  mcp:   http://localhost:$MCP_PORT/mcp  (auth required)"
else
echo "  │  mcp:   skipped (no token; set BETS_MCP_TOKEN or"
echo "  │         create .mcp-token; or BETS_DISABLE_MCP=1)"
fi
echo "  │                                                      │"
echo "  │  press Ctrl+C to stop both                           │"
echo "  └──────────────────────────────────────────────────────┘"
echo

# 6. Background MCP (with output prefixed [mcp]) if configured.
MCP_PID=""
if [ "$START_MCP" = "1" ]; then
    BETS_MCP_HOST="${BETS_MCP_HOST:-0.0.0.0}" \
    BETS_MCP_PORT="$MCP_PORT" \
    BETS_MCP_API_BASE="${BETS_MCP_API_BASE:-http://127.0.0.1:$PORT}" \
    python -u -m backend.mcp_server 2>&1 | sed -u 's/^/[mcp] /' &
    MCP_PID=$!
fi

# Trap Ctrl+C / shell exit so we kill the MCP child when FastAPI stops.
cleanup() {
    if [ -n "$MCP_PID" ]; then
        # Kill the entire pipeline (python + sed) cleanly.
        kill -TERM "$MCP_PID" 2>/dev/null || true
        # The sed wrapper's PID = MCP_PID; the python under it is its child.
        pkill -TERM -P "$MCP_PID" 2>/dev/null || true
    fi
}
trap cleanup INT TERM EXIT

# 7. Foreground FastAPI (this is the process you Ctrl+C). Output prefixed
# [api] so it's distinguishable from the [mcp] interleaved lines.
python -u -m uvicorn backend.main:app --reload --port "$PORT" 2>&1 | sed -u 's/^/[api] /'
