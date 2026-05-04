#!/usr/bin/env bash
# Kill any running backend and start a fresh one.
set -euo pipefail
cd "$(dirname "$0")"

echo "[bets] stopping any running backend ..."
pkill -f "uvicorn backend.main" 2>/dev/null || true
sleep 1

exec ./start.sh
