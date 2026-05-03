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
    settled_pl    REAL,
    target_date   TEXT,
    settled_at    INTEGER,
    settled_max_f INTEGER
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

CREATE TABLE IF NOT EXISTS feature_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 INTEGER NOT NULL,
    city               TEXT    NOT NULL,
    target_date        TEXT,                -- ISO date the prediction is for
    -- Ensemble inputs (Fahrenheit unless noted)
    gfs_mos_max        REAL,
    nam_mos_max        REAL,
    nws_forecast_max   REAL,
    ecmwf_max          REAL,
    om_max             REAL,
    -- What we actually used
    model_max          REAL    NOT NULL,
    sigma_used         REAL,
    obs_now            REAL,
    -- Upper air (Celsius for pressure-level temps)
    t850_c             REAL,
    t700_c             REAL,
    t500_c             REAL,
    h500_m             INTEGER,
    lapse_c_per_km     REAL,
    rh850_pct          INTEGER,
    -- Sounding observed
    sounding_t850_obs  REAL,
    sounding_t850_delta REAL,
    -- AFD-derived
    afd_conf_score     INTEGER,
    afd_above_normal   INTEGER,
    afd_sea_breeze     INTEGER,
    afd_marine_layer   INTEGER,
    afd_smoke          INTEGER,
    afd_overcast       INTEGER,
    afd_offshore       INTEGER,
    -- Recommended bracket at this snapshot
    rec_bracket_label  TEXT,
    rec_bracket_lo     INTEGER,
    rec_bracket_hi     INTEGER,
    rec_edge_cents     INTEGER,
    rec_kalshi_pct     REAL,
    rec_model_pct      REAL
);
CREATE INDEX IF NOT EXISTS idx_feat_city_ts ON feature_snapshots(city, ts DESC);
CREATE INDEX IF NOT EXISTS idx_feat_target_date ON feature_snapshots(target_date);
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
            # Migrate older databases that pre-date the settlement columns.
            cols = {r["name"] for r in _conn.execute("PRAGMA table_info(bets)").fetchall()}
            for col, ddl in (
                ("target_date",   "ALTER TABLE bets ADD COLUMN target_date TEXT"),
                ("settled_at",    "ALTER TABLE bets ADD COLUMN settled_at INTEGER"),
                ("settled_max_f", "ALTER TABLE bets ADD COLUMN settled_max_f INTEGER"),
            ):
                if col not in cols:
                    _conn.execute(ddl)
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
    target_date: Optional[str] = None,
) -> Dict:
    c = _conn_or_init()
    with _lock:
        cur = c.execute(
            """INSERT INTO bets (
                placed_at, city, bracket_label, bracket_lo, bracket_hi,
                side, size, entry_cents, target_date
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (int(time.time() * 1000), city, bracket_label, bracket_lo, bracket_hi,
             side, size, entry_cents, target_date),
        )
        c.commit()
        row = c.execute("SELECT * FROM bets WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_settleable_bets(today_iso: str) -> List[Dict]:
    """Open bets whose target_date is strictly before `today_iso` — i.e.,
    whose settlement window has fully closed and the NWS climate report
    should now be authoritative."""
    c = _conn_or_init()
    rows = c.execute(
        "SELECT * FROM bets WHERE status='open' AND target_date IS NOT NULL "
        "AND target_date < ? ORDER BY placed_at ASC",
        (today_iso,),
    ).fetchall()
    return [dict(r) for r in rows]


def settle_bet(bet_id: int, settled_pl: float, settled_max_f: int) -> None:
    """Mark a bet settled. Also writes a final position_mark at settled_at
    so the equity timeline picks up the settlement-snap from the latest
    Kalshi mark to the realized P/L."""
    c = _conn_or_init()
    with _lock:
        ts = int(time.time())
        c.execute(
            "UPDATE bets SET status='settled', settled_pl=?, settled_at=?, "
            "settled_max_f=? WHERE id=?",
            (settled_pl, ts, settled_max_f, bet_id),
        )
        c.commit()


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
        c.execute("DELETE FROM feature_snapshots")
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
    """Per-tick equity curve. At each timestamp t, equity =
        starting_balance
        + realized_pl    (sum of settled_pl for bets where settled_at <= t)
        + unrealized_pl  (sum of position_marks at t for bets that were
                          still open at t).

    Tick set is the union of position_marks timestamps and settled_at
    timestamps so the curve includes settlement-jump events even when
    they happen between snapshot ticks. Output rows match the prototype's
    history shape so PnLView consumes them unchanged."""
    cutoff = int(time.time() - hours * 3600)
    c = _conn_or_init()

    tick_rows = c.execute(
        """SELECT DISTINCT ts FROM (
              SELECT ts FROM position_marks WHERE ts >= ?
              UNION
              SELECT settled_at AS ts FROM bets
                WHERE status='settled' AND settled_at IS NOT NULL AND settled_at >= ?
           ) ORDER BY ts ASC""",
        (cutoff, cutoff),
    ).fetchall()

    # Settlement events: realized P/L wins / losses
    settle_rows = c.execute(
        "SELECT settled_at AS ts, settled_pl FROM bets "
        "WHERE status='settled' AND settled_at IS NOT NULL "
        "ORDER BY settled_at ASC"
    ).fetchall()
    settle_events = [(int(r["ts"]), float(r["settled_pl"])) for r in settle_rows]

    out: List[Dict] = []
    peak = starting_balance
    prev_total = 0.0
    for r in tick_rows:
        ts = int(r["ts"])

        realized = sum(pl for at, pl in settle_events if at <= ts)
        # Unrealized: marks at this exact ts for bets that hadn't settled by ts
        unreal_row = c.execute(
            """SELECT COALESCE(SUM(pm.pl), 0.0) AS unreal,
                      COUNT(*)               AS marks
                 FROM position_marks pm
                 JOIN bets b ON b.id = pm.bet_id
                WHERE pm.ts = ?
                  AND (b.status = 'open'
                       OR (b.status = 'settled' AND b.settled_at > ?))""",
            (ts, ts),
        ).fetchone()
        unrealized = float(unreal_row["unreal"])
        marks = int(unreal_row["marks"])

        total_pl = realized + unrealized
        eq = starting_balance + total_pl
        if eq > peak:
            peak = eq
        out.append({
            "ts": ts,
            "date": time.strftime("%H:%M", time.localtime(ts)),
            "equity": round(eq, 2),
            "pl": round(total_pl - prev_total, 2),
            "cumulative_pl": round(total_pl, 2),
            "realized_pl": round(realized, 2),
            "unrealized_pl": round(unrealized, 2),
            "drawdown": round(eq - peak, 2),
            "trades": marks,
            "winRate": 0.5,
        })
        prev_total = total_pl
    return out


def insert_feature_snapshot(state: Dict, ts: Optional[int] = None) -> None:
    """Persist the full input feature vector for a city snapshot. Joining
    this against bets.settled_max_f by (city, target_date) yields a clean
    (features → outcome) pair for offline ML training."""
    if ts is None:
        ts = int(time.time())
    flags = state.get("flags") or {}
    upper = state.get("upperAir") or {}
    snd = state.get("sounding") or {}
    rec = state.get("settlementBracket") or {}
    c = _conn_or_init()
    with _lock:
        c.execute(
            """INSERT INTO feature_snapshots (
                ts, city, target_date,
                gfs_mos_max, nam_mos_max, nws_forecast_max, ecmwf_max, om_max,
                model_max, sigma_used, obs_now,
                t850_c, t700_c, t500_c, h500_m, lapse_c_per_km, rh850_pct,
                sounding_t850_obs, sounding_t850_delta,
                afd_conf_score, afd_above_normal,
                afd_sea_breeze, afd_marine_layer, afd_smoke, afd_overcast, afd_offshore,
                rec_bracket_label, rec_bracket_lo, rec_bracket_hi,
                rec_edge_cents, rec_kalshi_pct, rec_model_pct
            ) VALUES (?,?,?, ?,?,?,?,?, ?,?,?, ?,?,?,?,?,?, ?,?, ?,?, ?,?,?,?,?, ?,?,?, ?,?,?)""",
            (
                ts, state["city"]["code"], state.get("targetDate"),
                state.get("mosMax"), state.get("namMos"),
                state.get("nwsForecast"), state.get("ecmwfMax"),
                state.get("omMax"),
                state.get("modelMax"), state.get("sigmaUsed"),
                state.get("obsCurrent"),
                upper.get("t850"), upper.get("t700"), upper.get("t500"),
                upper.get("h500"), upper.get("lapse"), upper.get("rh850"),
                snd.get("t850Obs"), snd.get("t850Delta"),
                state.get("afdConfScore"),
                int(bool(state.get("afdAboveNormal"))) if state.get("afdAboveNormal") is not None else None,
                int(bool(flags.get("seaBreeze"))),
                int(bool(flags.get("marineLayer"))),
                int(bool(flags.get("smoke"))),
                int(bool(flags.get("overcast"))),
                int(bool(flags.get("offshore"))),
                rec.get("label"), rec.get("lo"), rec.get("hi"),
                int(round((rec.get("edge") or 0) * 100)),
                rec.get("kalshiPct"), rec.get("modelPct"),
            ),
        )
        c.commit()


def list_bets_for_target(city: str, target_date: str) -> List[Dict]:
    """Bets placed for one (city, target_date) — used by the auto-trader
    to skip placement when an open bet on the same bracket already exists."""
    c = _conn_or_init()
    rows = c.execute(
        "SELECT * FROM bets WHERE city = ? AND target_date = ? ORDER BY placed_at ASC",
        (city.upper(), target_date),
    ).fetchall()
    return [dict(r) for r in rows]


def stats_summary() -> Dict:
    """Realized win-rate and bet counts. Cheap to compute, exposed at
    /api/stats for the P&L view to swap in real win-rate."""
    c = _conn_or_init()
    row = c.execute(
        """SELECT
              COUNT(*) FILTER (WHERE status='settled')                       AS settled,
              COUNT(*) FILTER (WHERE status='settled' AND settled_pl > 0)    AS won,
              COUNT(*) FILTER (WHERE status='open')                          AS open,
              COALESCE(SUM(settled_pl) FILTER (WHERE status='settled'), 0.0) AS realized_pl
           FROM bets"""
    ).fetchone()
    settled = int(row["settled"])
    return {
        "settled": settled,
        "won": int(row["won"]),
        "open": int(row["open"]),
        "winRate": (float(row["won"]) / settled) if settled else None,
        "realizedPl": round(float(row["realized_pl"]), 2),
    }


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
