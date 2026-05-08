#!/usr/bin/env bash
# One-command boot for the Bets stack.
#
#   ./start.sh
#
# Idempotent: creates .venv, installs deps, seeds backend/.env from the
# template if missing, then launches both servers:
#   - FastAPI  on port 8000  (serves /api/* and the UI)        [api]
#   - MCP      on port 5000  (Claude Desktop / Code tools)     [mcp]
#
# MCP lifecycle is DETACHED from the API. Ctrl+C stops only the API;
# MCP keeps running so Claude Desktop's mcp-remote stays connected
# across API code reloads. If you re-run start.sh and MCP is already
# listening on its port, the existing instance is left alone (you'll
# see "[bets] MCP already running on :5000 — leaving it alone").
#
# Logs:
#   - API runs in your terminal foreground
#   - MCP runs detached, logs to /tmp/bets-mcp.log
#
# To force-restart MCP (e.g. after editing backend/mcp_server.py or
# rotating the token):     pkill -f "backend.mcp_server"   ./start.sh
# To stop EVERYTHING:                                       ./stop.sh
#
# Skip MCP launch entirely with BETS_DISABLE_MCP=1.

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
DEPS_MARKER="$VENV/.deps-installed"
ENV_FILE="backend/.env"
PORT="${PORT:-8000}"
MCP_PORT="${BETS_MCP_PORT:-5000}"
MCP_LOG="/tmp/bets-mcp.log"

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

# 5. MCP launch (detached, idempotent). If something is already
# listening on the MCP port, leave it alone — restarting would invalidate
# every active session ID and force Claude Desktop to reconnect.
MCP_STATUS="running"
if [ "$START_MCP" = "1" ]; then
    if ss -ltn 2>/dev/null | grep -q ":${MCP_PORT} "; then
        echo "[bets] MCP already running on :${MCP_PORT} — leaving it alone"
        MCP_STATUS="reused"
    else
        echo "[bets] launching MCP detached → logs at $MCP_LOG"
        BETS_MCP_HOST="${BETS_MCP_HOST:-0.0.0.0}" \
        BETS_MCP_PORT="$MCP_PORT" \
        BETS_MCP_API_BASE="${BETS_MCP_API_BASE:-http://127.0.0.1:$PORT}" \
        nohup python -u -m backend.mcp_server > "$MCP_LOG" 2>&1 &
        disown
        MCP_STATUS="started"
        # Brief wait so the banner reflects the actual binding state.
        for _ in 1 2 3 4 5 6 7 8; do
            ss -ltn 2>/dev/null | grep -q ":${MCP_PORT} " && break
            sleep 0.5
        done
    fi
else
    MCP_STATUS="skipped"
fi

# 6. Banner
echo
echo "  ┌──────────────────────────────────────────────────────┐"
echo "  │  Bets — Kalshi weather trading terminal              │"
echo "  │                                                      │"
echo "  │  open:  http://localhost:$PORT/Bets.html"
echo "  │  api:   http://localhost:$PORT/api/health"
case "$MCP_STATUS" in
    running|started)
        echo "  │  mcp:   http://localhost:$MCP_PORT/mcp  (auth required, persistent)"
        ;;
    reused)
        echo "  │  mcp:   http://localhost:$MCP_PORT/mcp  (already up — reused)"
        ;;
    skipped)
        echo "  │  mcp:   skipped (no token; set BETS_MCP_TOKEN or"
        echo "  │         create .mcp-token; or BETS_DISABLE_MCP=1)"
        ;;
esac
echo "  │                                                      │"
echo "  │  Ctrl+C stops the API only — MCP keeps running       │"
echo "  │  ./stop.sh stops everything                          │"
echo "  └──────────────────────────────────────────────────────┘"
echo

# 7. Foreground FastAPI. Output prefixed [api] so it's distinguishable.
# NO trap on the MCP — it stays detached so Claude Desktop's mcp-remote
# stays connected across `git pull && ./start.sh` cycles.
exec python -u -m uvicorn backend.main:app --reload --port "$PORT" 2>&1 | sed -u 's/^/[api] /'
