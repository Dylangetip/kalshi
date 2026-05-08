#!/usr/bin/env bash
# Stop the entire Bets stack (API + MCP).
#
# start.sh leaves MCP running across API restarts so Claude Desktop /
# Code stays connected. When you actually want to bring everything down,
# this script kills both.

set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
MCP_PORT="${BETS_MCP_PORT:-5000}"

echo "[bets] stopping API ..."
pkill -TERM -f "uvicorn backend.main" 2>/dev/null || true

echo "[bets] stopping MCP ..."
pkill -TERM -f "backend.mcp_server" 2>/dev/null || true

# Give them ~2s to exit cleanly, then SIGKILL stragglers.
sleep 2
pkill -KILL -f "uvicorn backend.main" 2>/dev/null || true
pkill -KILL -f "backend.mcp_server" 2>/dev/null || true

# Belt-and-braces: if something else is squatting on either port, free it.
fuser -k -TERM "${PORT}/tcp" 2>/dev/null || true
fuser -k -TERM "${MCP_PORT}/tcp" 2>/dev/null || true

# Confirm
sleep 1
if ss -ltn 2>/dev/null | grep -qE ":(${PORT}|${MCP_PORT}) "; then
    echo "[bets] WARNING: still something listening:"
    ss -ltn | grep -E ":(${PORT}|${MCP_PORT}) " || true
else
    echo "[bets] all stopped"
fi
