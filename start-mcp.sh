#!/usr/bin/env bash
# Launch the MCP server (Model Context Protocol) so Claude Desktop / Code
# can run tools against the bets stack. Runs alongside ./start.sh — they
# talk to the same SQLite DB and the MCP server proxies to the FastAPI
# server on localhost for any action that has to go through the existing
# safety guards (e.g. /api/kalshi/order's $25 cap).
#
# First time:
#   1. ./start.sh          (in another terminal — needs port 8000)
#   2. Generate a token:    openssl rand -hex 24 > .mcp-token
#   3. ./start-mcp.sh
#
# Then in Claude Desktop's claude_desktop_config.json:
#   {
#     "mcpServers": {
#       "bets": {
#         "command": "npx",
#         "args": [
#           "mcp-remote",
#           "http://YOUR-HOST:5000/mcp",
#           "--header", "Authorization: Bearer ${BETS_MCP_TOKEN}"
#         ],
#         "env": {"BETS_MCP_TOKEN": "<paste contents of .mcp-token>"}
#       }
#     }
#   }

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
PORT="${BETS_MCP_PORT:-5000}"
HOST="${BETS_MCP_HOST:-0.0.0.0}"
API_BASE="${BETS_MCP_API_BASE:-http://127.0.0.1:8000}"

if [ ! -d "$VENV" ]; then
    echo "[mcp] no $VENV — run ./start.sh first to provision it"
    exit 1
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

# Refresh deps if requirements.txt grew the fastmcp line since the last install
if ! python -c "import fastmcp" 2>/dev/null; then
    echo "[mcp] fastmcp not installed — installing dependencies ..."
    pip install -q -r backend/requirements.txt
fi

# Pull token from .mcp-token if BETS_MCP_TOKEN isn't already set in env.
if [ -z "${BETS_MCP_TOKEN:-}" ] && [ -f .mcp-token ]; then
    BETS_MCP_TOKEN="$(tr -d '[:space:]' < .mcp-token)"
    export BETS_MCP_TOKEN
fi

if [ -z "${BETS_MCP_TOKEN:-}" ] && [ "${BETS_MCP_ALLOW_NO_AUTH:-0}" != "1" ]; then
    cat <<'EOF'
[mcp] No BETS_MCP_TOKEN set and BETS_MCP_ALLOW_NO_AUTH != 1.
[mcp] The server will refuse every request until you fix one of these:
[mcp]
[mcp]   # generate a token (recommended)
[mcp]   openssl rand -hex 24 > .mcp-token
[mcp]
[mcp]   # OR explicitly disable auth (NOT for networked use)
[mcp]   BETS_MCP_ALLOW_NO_AUTH=1 ./start-mcp.sh
EOF
    exit 1
fi

if [ -n "${BETS_MCP_TOKEN:-}" ]; then
    AUTH_DISP="bearer (configured)"
else
    AUTH_DISP="DISABLED via BETS_MCP_ALLOW_NO_AUTH=1"
fi
echo
echo "  ┌──────────────────────────────────────────────────────┐"
echo "  │  Bets MCP server                                     │"
echo "  │                                                      │"
echo "  │  url:    http://${HOST}:${PORT}/mcp"
echo "  │  proxy:  ${API_BASE}"
echo "  │  auth:   ${AUTH_DISP}"
echo "  │                                                      │"
echo "  │  press Ctrl+C to stop                                │"
echo "  └──────────────────────────────────────────────────────┘"
echo

export BETS_MCP_HOST="$HOST"
export BETS_MCP_PORT="$PORT"
export BETS_MCP_API_BASE="$API_BASE"
exec python -m backend.mcp_server
