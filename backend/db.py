"""SQLite persistence for placed bets.

Schema is intentionally tiny:
- bets: every bet placed via POST /api/bets, durable across restarts.
  entry_cents stores the YES price at entry regardless of side, so
  marking-to-market against a current YES Kalshi probability is a
  straightforward subtraction. `status` is reserved for a future
  settlement job (when we wire the NWS Daily Climate Report in).

WAL mode keeps reads non-blocking while a snapshot writer lands rows;
sqlite3 is shared across threads with check_same_thread=False because
FastAPI runs sync endpoints in a thread pool.
"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

DB_PATH = Path(__file__).parent / "bets.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    placed_at     INTEGER NOT NULL,
    city          TEXT    NOT NULL,
    bracket_label TEXT    NOT NULL,
    bracket_lo    INTEGER NOT NULL,
    bracket_hi    INTEGER NOT NULL,
    side          TEXT    NOT NULL CHECK (side IN ('YES','NO')),
    size          INTEGER NOT NULL,
    entry_cents   INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'open',
    settled_pl    REAL
);
CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);
CREATE INDEX IF NOT EXISTS idx_bets_placed_at ON bets(placed_at DESC);

CREATE TABLE IF NOT EXISTS snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 INTEGER NOT NULL,
    city               TEXT    NOT NULL,
    model_max          REAL    NOT NULL,
    best_edge_cents    INTEGER NOT NULL,
    best_bracket_label TEXT    NOT NULL,
    best_kalshi_pct    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_city_ts ON snapshots(city, ts DESC);

CREATE TABLE IF NOT EXISTS position_marks (
    bet_id          INTEGER NOT NULL,
    ts              INTEGER NOT NULL,
    current_yes_pct REAL    NOT NULL,
    pl              REAL    NOT NULL,
    PRIMARY KEY (bet_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_marks_ts ON position_marks(ts);
"""

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init() -> None:
    global _conn
    with _lock:
        if _conn is None:
            _conn = _connect()
            _conn.executescript(SCHEMA)
            _conn.commit()


def _conn_or_init() -> sqlite3.Connection:
    if _conn is None:
        init()
    assert _conn is not None
    return _conn


def insert_bet(
    city: str,
    bracket_label: str,
    bracket_lo: int,
    bracket_hi: int,
    side: str,
    size: int,
    entry_cents: int,
) -> Dict:
    c = _conn_or_init()
    with _lock:
        cur = c.execute(
            """INSERT INTO bets (
                placed_at, city, bracket_label, bracket_lo, bracket_hi,
                side, size, entry_cents
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (int(time.time() * 1000), city, bracket_label, bracket_lo, bracket_hi,
             side, size, entry_cents),
        )
        c.commit()
        row = c.execute("SELECT * FROM bets WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_bets(limit: int = 200) -> List[Dict]:
    c = _conn_or_init()
    rows = c.execute(
        "SELECT * FROM bets ORDER BY placed_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_open_bets() -> List[Dict]:
    c = _conn_or_init()
    rows = c.execute(
        "SELECT * FROM bets WHERE status='open' ORDER BY placed_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def reset() -> None:
    """Test helper: wipe the bets table."""
    c = _conn_or_init()
    with _lock:
        c.execute("DELETE FROM bets")
        c.execute("DELETE FROM snapshots")
        c.execute("DELETE FROM position_marks")
        c.commit()


def insert_snapshot(state: Dict, ts: Optional[int] = None) -> None:
    """Persist a city snapshot. Pulls model_max + recommended bracket from
    the same response shape /api/state returns, so the caller doesn't need
    to reshape data. Pass `ts` explicitly to keep all cities in a snapshot
    loop iteration synchronized to the same wall-clock second."""
    if ts is None:
        ts = int(time.time())
    best = state["settlementBracket"]
    c = _conn_or_init()
    with _lock:
        c.execute(
            """INSERT INTO snapshots (
                ts, city, model_max, best_edge_cents,
                best_bracket_label, best_kalshi_pct
            ) VALUES (?,?,?,?,?,?)""",
            (
                ts,
                state["city"]["code"],
                float(state["modelMax"]),
                int(state["bestEdgeCents"]),
                str(best["label"]),
                float(best["kalshiPct"]),
            ),
        )
        c.commit()


def insert_position_mark(bet_id: int, ts: int, current_yes_pct: float, pl: float) -> None:
    """Mark a single open bet at a snapshot tick. INSERT OR REPLACE so reruns
    of the same loop iteration are idempotent."""
    c = _conn_or_init()
    with _lock:
        c.execute(
            "INSERT OR REPLACE INTO position_marks (bet_id, ts, current_yes_pct, pl) "
            "VALUES (?,?,?,?)",
            (bet_id, ts, current_yes_pct, pl),
        )
        c.commit()


def list_equity_points(hours: float = 72.0, starting_balance: float = 10000.0) -> List[Dict]:
    """Per-tick equity curve: starting bankroll + sum of open-position P/L
    at each snapshot timestamp. Output shape matches the prototype's
    history rows (date, equity, pl, drawdown, trades) so PnLView's chart
    components consume it without reshaping."""
    cutoff = int(time.time() - hours * 3600)
    c = _conn_or_init()
    rows = c.execute(
        "SELECT ts, SUM(pl) AS total_pl, COUNT(*) AS marks "
        "FROM position_marks WHERE ts >= ? GROUP BY ts ORDER BY ts ASC",
        (cutoff,),
    ).fetchall()
    out: List[Dict] = []
    peak = starting_balance
    prev_total = 0.0
    for r in rows:
        total_pl = float(r["total_pl"] or 0.0)
        eq = starting_balance + total_pl
        if eq > peak:
            peak = eq
        out.append({
            "ts": r["ts"],
            "date": time.strftime("%H:%M", time.localtime(r["ts"])),
            "equity": round(eq, 2),
            "pl": round(total_pl - prev_total, 2),
            "cumulative_pl": round(total_pl, 2),
            "drawdown": round(eq - peak, 2),
            "trades": int(r["marks"]),
            "winRate": 0.5,  # placeholder until settlement is wired
        })
        prev_total = total_pl
    return out


def list_snapshots(city: str, hours: float = 24.0, limit: int = 500) -> List[Dict]:
    cutoff = int(time.time() - hours * 3600)
    c = _conn_or_init()
    rows = c.execute(
        "SELECT ts, model_max, best_edge_cents, best_bracket_label, best_kalshi_pct "
        "FROM snapshots WHERE city = ? AND ts >= ? ORDER BY ts ASC, id ASC LIMIT ?",
        (city.upper(), cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_snapshot(city: str) -> Optional[Dict]:
    c = _conn_or_init()
    row = c.execute(
        "SELECT ts, model_max, best_edge_cents, best_bracket_label, best_kalshi_pct "
        "FROM snapshots WHERE city = ? ORDER BY ts DESC, id DESC LIMIT 1",
        (city.upper(),),
    ).fetchone()
    return dict(row) if row else None
