"""MCP (Model Context Protocol) server for the Bets stack.

Exposes a curated set of tools so Claude Desktop / Claude Code can read the
bets database, inspect runtime state, mutate auto-trader and bias config,
and (optionally) place real Kalshi orders. Designed to run alongside the
main FastAPI server (which it talks to over HTTP for any action that has
to go through the existing FastAPI safety guards — e.g. /api/kalshi/order's
$25 cap).

Run:
    BETS_MCP_TOKEN=your-long-random-string \\
    .venv/bin/python -m backend.mcp_server

The server listens on http://0.0.0.0:5000/mcp by default. Override port with
BETS_MCP_PORT. Disable auth (NOT recommended for networked use) by leaving
BETS_MCP_TOKEN unset.

Claude Desktop config (add to claude_desktop_config.json):
    {
      "mcpServers": {
        "bets": {
          "command": "npx",
          "args": [
            "mcp-remote",
            "http://YOUR-HOST:5000/mcp",
            "--header", "Authorization: Bearer ${BETS_MCP_TOKEN}"
          ],
          "env": {"BETS_MCP_TOKEN": "your-long-random-string"}
        }
      }
    }
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

try:
    from fastmcp import FastMCP
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
except ImportError as exc:  # noqa: BLE001
    raise SystemExit(
        "fastmcp not installed. Run:  pip install -r backend/requirements.txt\n"
        f"(import error: {exc})"
    )

from . import db


# ── Config ──────────────────────────────────────────────────────────────────

BETS_API_BASE = os.getenv("BETS_MCP_API_BASE", "http://127.0.0.1:8000")
MCP_HOST = os.getenv("BETS_MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("BETS_MCP_PORT", "5000"))
MCP_TOKEN = os.getenv("BETS_MCP_TOKEN") or None
SQL_ROW_CAP = int(os.getenv("BETS_MCP_SQL_ROW_CAP", "1000"))

_SQL_FORBIDDEN_TOKENS = (
    "insert ", "update ", "delete ", "drop ", "alter ", "create ",
    "replace ", "attach ", "detach ", "vacuum", "pragma ",
)


def _strip_sql_comments(q: str) -> str:
    out, i, n, in_block = [], 0, len(q), False
    while i < n:
        if in_block:
            if q[i:i+2] == "*/": in_block = False; i += 2
            else: i += 1
            continue
        if q[i:i+2] == "--":
            j = q.find("\n", i)
            if j == -1: break
            i = j + 1; continue
        if q[i:i+2] == "/*": in_block = True; i += 2; continue
        out.append(q[i]); i += 1
    return "".join(out)


def _validate_sql(query: str) -> str:
    raw = (query or "").strip().rstrip(";").strip()
    if not raw:
        raise ValueError("empty query")
    cleaned = _strip_sql_comments(raw).strip()
    cl = cleaned.lower()
    if ";" in cleaned:
        raise ValueError("only a single statement is allowed")
    if not (cl.startswith("select") or cl.startswith("with")):
        raise ValueError("only SELECT (or WITH ... SELECT) queries are allowed")
    for tok in _SQL_FORBIDDEN_TOKENS:
        if tok in cl:
            raise ValueError(f"keyword not allowed in read-only console: {tok.strip()}")
    return raw


# ── Auth middleware ─────────────────────────────────────────────────────────

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any /mcp request without a matching Authorization: Bearer ..."""

    def __init__(self, app, token: Optional[str]):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        # Only gate the MCP endpoint; allow everything else through.
        if not request.url.path.startswith("/mcp"):
            return await call_next(request)
        if not self.token:
            # Unauthenticated mode — only allow if explicitly opted in.
            if os.getenv("BETS_MCP_ALLOW_NO_AUTH") != "1":
                return JSONResponse(
                    {"error": "auth not configured. Set BETS_MCP_TOKEN or BETS_MCP_ALLOW_NO_AUTH=1"},
                    status_code=503,
                )
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        presented = header[7:].strip()
        if not presented or presented != self.token:
            return JSONResponse({"error": "invalid token"}, status_code=401)
        return await call_next(request)


# ── Server + tools ──────────────────────────────────────────────────────────

mcp = FastMCP("bets")


# ─── Read-only SQL ──────────────────────────────────────────────────────────

class QueryDbIn(BaseModel):
    query: str = Field(..., description="A single SELECT or WITH...SELECT statement.")


@mcp.tool()
def query_db(args: QueryDbIn) -> Dict[str, Any]:
    """Run a read-only SELECT against the bets SQLite database.

    Hard rules: single statement, must start with SELECT or WITH, no
    DML/DDL keywords, ≤ SQL_ROW_CAP rows returned. Connection is opened
    in ?mode=ro so even a parser bypass cannot mutate.

    Returns: columns, rows, row_count, truncated, query_ms.
    """
    raw = _validate_sql(args.query)
    uri = f"file:{db.DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        import time as _t
        t0 = _t.time()
        try:
            cur = conn.execute(raw)
        except sqlite3.Error as exc:
            return {"error": f"sql error: {exc}"}
        rows_raw = cur.fetchmany(SQL_ROW_CAP + 1)
        truncated = len(rows_raw) > SQL_ROW_CAP
        rows_raw = rows_raw[:SQL_ROW_CAP]
        columns = [d[0] for d in (cur.description or [])]
        rows = [{c: r[c] for c in columns} for r in rows_raw]
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "query_ms": int((_t.time() - t0) * 1000),
            "row_cap": SQL_ROW_CAP,
        }
    finally:
        conn.close()


# ─── Status / diagnostics (proxy to FastAPI) ────────────────────────────────

def _api_get(path: str, **params) -> Dict[str, Any]:
    try:
        r = httpx.get(f"{BETS_API_BASE}{path}", params=params, timeout=10.0)
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text, "status": r.status_code}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"upstream call failed: {exc}"}


def _api_post(path: str, body: Dict) -> Dict[str, Any]:
    try:
        r = httpx.post(f"{BETS_API_BASE}{path}", json=body, timeout=15.0)
        if r.headers.get("content-type", "").startswith("application/json"):
            j = r.json()
            if r.status_code >= 400 and isinstance(j, dict):
                j.setdefault("status_code", r.status_code)
            return j
        return {"text": r.text, "status_code": r.status_code}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"upstream call failed: {exc}"}


@mcp.tool()
def get_status() -> Dict[str, Any]:
    """Snapshot of account + auto-trader runtime: bankroll, available cash,
    total equity, last-run stats, current sizing config, bias settings."""
    return _api_get("/api/auto-trade/info")


@mcp.tool()
def get_stats() -> Dict[str, Any]:
    """Realized win-rate, settled count, open count, realized P/L, open
    stakes. Same shape as /api/stats."""
    return _api_get("/api/stats")


@mcp.tool()
def get_bias() -> Dict[str, Any]:
    """Per-(city, horizon-bucket) forecast bias values + cache age + the
    correction toggles (enabled, half-life, lookback, max correction)."""
    return _api_get("/api/bias")


@mcp.tool()
def get_state() -> List[Dict[str, Any]]:
    """Live forecast state for every city. Each entry has model_max,
    rawModelMax, biasShift, top settlement bracket, all bracket prices,
    and current edge in cents."""
    return _api_get("/api/state")  # type: ignore[return-value]


@mcp.tool()
def get_health() -> Dict[str, Any]:
    """Liveness check: city codes, open-bet count, status='ok'."""
    return _api_get("/api/health")


class RecentBetsIn(BaseModel):
    limit: int = Field(50, ge=1, le=500, description="Max rows to return.")
    status: Optional[str] = Field(
        None,
        description="Filter to 'open' or 'settled'. Omit for all.",
    )


@mcp.tool()
def get_recent_bets(args: RecentBetsIn) -> List[Dict[str, Any]]:
    """Recent bets newest-first, optionally filtered by status."""
    sql = "SELECT * FROM bets"
    if args.status in ("open", "settled"):
        sql += f" WHERE status = '{args.status}'"
    sql += f" ORDER BY placed_at DESC LIMIT {int(args.limit)}"
    res = query_db(QueryDbIn(query=sql))
    if "error" in res:
        return [{"error": res["error"]}]
    return res["rows"]


# ─── Config mutators (proxy to FastAPI) ─────────────────────────────────────

class AutoTradeConfigPatch(BaseModel):
    enabled: Optional[bool] = None
    min_edge_cents: Optional[int] = Field(None, ge=0, le=100)
    bankroll: Optional[float] = Field(None, gt=0)
    max_usd: Optional[float] = Field(None, gt=0)
    max_pct_of_balance: Optional[float] = Field(None, ge=0, le=1)
    min_usd: Optional[float] = Field(None, ge=0)
    interval_seconds: Optional[int] = Field(None, ge=10, le=86400)
    max_bets_per_city_per_day: Optional[int] = Field(None, ge=1, le=20)
    min_model_pct: Optional[float] = Field(None, ge=0, le=1)
    min_kelly: Optional[float] = Field(None, ge=0, le=1)
    skip_entry_below_cents: Optional[int] = Field(None, ge=0, le=100)
    bet_sizing_mode: Optional[str] = Field(None, pattern="^(kelly|tiers)$")
    size_tiers: Optional[List[Dict[str, float]]] = None


@mcp.tool()
def set_auto_trade_config(patch: AutoTradeConfigPatch) -> Dict[str, Any]:
    """Update auto-trader runtime config. All fields optional; only the
    keys you set get applied. Takes effect on the next 10-min loop tick."""
    body = patch.model_dump(exclude_none=True)
    if not body:
        return {"error": "empty patch"}
    return _api_post("/api/auto-trade/config", body)


class BiasConfigPatch(BaseModel):
    enabled: Optional[bool] = None
    half_life_days: Optional[float] = Field(None, gt=0, le=365)
    lookback_days: Optional[int] = Field(None, gt=0, le=3650)
    max_correction: Optional[float] = Field(None, ge=0, le=20)


@mcp.tool()
def set_bias_config(patch: BiasConfigPatch) -> Dict[str, Any]:
    """Toggle bias correction on/off and tune its decay/lookback/cap."""
    body = patch.model_dump(exclude_none=True)
    if not body:
        return {"error": "empty patch"}
    return _api_post("/api/bias/config", body)


@mcp.tool()
def refresh_bias() -> Dict[str, Any]:
    """Invalidate the bias cache so the next forecast recomputes residuals
    from the fresh historical_predictions × historical_actuals join."""
    return _api_post("/api/bias/refresh", {})


class ModelConfigPatch(BaseModel):
    modelmax_source: Optional[str] = Field(
        None, pattern="^(auto|ml|blend|ensemble)$",
        description="Which forecast source to use as the active prediction.",
    )
    blend_alpha: Optional[float] = Field(
        None, ge=0, le=1,
        description="α in α·ML + (1−α)·Ensemble. Only used when source=blend or auto.",
    )


@mcp.tool()
def set_model_config(patch: ModelConfigPatch) -> Dict[str, Any]:
    """Switch active prediction source (auto/ml/blend/ensemble) and/or the
    blend α weight. Invalidates the city-state cache so changes show up
    immediately."""
    body = patch.model_dump(exclude_none=True)
    if not body:
        return {"error": "empty patch"}
    return _api_post("/api/model/config", body)


# ─── Real Kalshi order placement ────────────────────────────────────────────

class PlaceOrder(BaseModel):
    ticker: str = Field(..., min_length=1, description="Kalshi event-market ticker, e.g. KXHIGHNY-26MAY07-T87")
    side: str = Field(..., pattern="^(yes|no)$")
    count: int = Field(..., gt=0, description="Number of contracts.")
    yes_price: Optional[int] = Field(None, ge=1, le=99, description="Limit price for YES side (cents).")
    no_price: Optional[int] = Field(None, ge=1, le=99, description="Limit price for NO side (cents).")
    action: str = Field("buy", pattern="^(buy|sell)$")
    type: str = Field("limit", pattern="^(limit|market)$")
    confirm: bool = Field(False, description="Must be true to actually place. The /api/kalshi/order endpoint enforces this server-side.")


@mcp.tool()
def place_kalshi_order(order: PlaceOrder) -> Dict[str, Any]:
    """Submit a REAL Kalshi order. Real money.

    The server-side endpoint (/api/kalshi/order) requires confirm=true and
    enforces a hard cost cap via env BETS_MAX_ORDER_USD (default $25). This
    tool just forwards to that endpoint — the Python guardrails apply. If
    Kalshi creds aren't configured, the call returns a 503 explaining what
    env vars to set.
    """
    return _api_post("/api/kalshi/order", order.model_dump())


@mcp.tool()
def get_kalshi_balance() -> Dict[str, Any]:
    """Live Kalshi account balance (sanity check before placing real orders).
    503 if Kalshi creds aren't configured."""
    return _api_get("/api/kalshi/balance")


# ─── Lifecycle ──────────────────────────────────────────────────────────────

def main() -> None:
    db.init()
    if not MCP_TOKEN and os.getenv("BETS_MCP_ALLOW_NO_AUTH") != "1":
        print(
            "[mcp] WARNING: BETS_MCP_TOKEN is unset and BETS_MCP_ALLOW_NO_AUTH != 1.\n"
            "[mcp] Server will refuse all /mcp requests until you set one of them."
        )
    print(f"[mcp] starting on http://{MCP_HOST}:{MCP_PORT}/mcp  (api={BETS_API_BASE})")
    # FastMCP exposes the underlying Starlette app via .http_app(); attach
    # bearer-auth middleware before serving so unauthorized requests are
    # rejected before they hit the MCP routing.
    app = mcp.http_app(path="/mcp")
    app.add_middleware(BearerAuthMiddleware, token=MCP_TOKEN)
    import uvicorn
    # access_log=False suppresses the per-request `INFO 200/404 ...` lines.
    # mcp-remote (Claude Desktop's bridge) repeatedly probes /performance
    # and POST / which 404 — useful info but floods the terminal. Real
    # errors still surface via log_level="info" on the application logger.
    # Override with BETS_MCP_ACCESS_LOG=1 if you ever want the chatter back.
    access_log = os.getenv("BETS_MCP_ACCESS_LOG", "0") == "1"
    uvicorn.run(
        app, host=MCP_HOST, port=MCP_PORT,
        log_level="info", access_log=access_log,
    )


if __name__ == "__main__":
    main()
