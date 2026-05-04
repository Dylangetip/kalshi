#!/usr/bin/env bash
# Kill any running backend and start a fresh one.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"

echo "[bets] stopping any process on port $PORT ..."
fuser -k "${PORT}/tcp" 2>/dev/null || true
pkill -f "uvicorn backend.main" 2>/dev/null || true

# Wait until the port is actually free (max 10s)
for i in $(seq 1 10); do
    if ! ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
        break
    fi
    sleep 1
done

exec ./start.sh

