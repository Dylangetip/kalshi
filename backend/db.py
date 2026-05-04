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

-- Historical (city, target_date, forecast_horizon) → past predictions.
-- Backfilled from Open-Meteo Historical Forecast API. Used as ML training X.
CREATE TABLE IF NOT EXISTS historical_predictions (
    city                    TEXT    NOT NULL,
    target_date             TEXT    NOT NULL,
    forecast_horizon_hours  INTEGER NOT NULL,
    gfs_max                 REAL,
    ecmwf_max               REAL,
    icon_max                REAL,
    om_max                  REAL,
    gfs_mos_max             REAL,
    nam_mos_max             REAL,
    t850_c                  REAL,
    t700_c                  REAL,
    t500_c                  REAL,
    h500_m                  INTEGER,
    rh850_pct               INTEGER,
    ensemble_max            REAL,
    PRIMARY KEY (city, target_date, forecast_horizon_hours)
);
CREATE INDEX IF NOT EXISTS idx_hpred_target ON historical_predictions(target_date);

-- Historical actual highs. From IEM ASOS hourly archive (no auth, no rate
-- limit). One row per (city, target_date). Used as ML training y.
CREATE TABLE IF NOT EXISTS historical_actuals (
    city          TEXT NOT NULL,
    target_date   TEXT NOT NULL,
    actual_max_f  REAL NOT NULL,
    source        TEXT NOT NULL,
    PRIMARY KEY (city, target_date)
);
CREATE INDEX IF NOT EXISTS idx_hact_target ON historical_actuals(target_date);

-- One row per training run. The model_path file contains the joblib pickle.
CREATE TABLE IF NOT EXISTS ml_runs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    trained_at            INTEGER NOT NULL,
    algorithm             TEXT    NOT NULL,
    n_train               INTEGER NOT NULL,
    n_test                INTEGER NOT NULL,
    train_mae             REAL,
    test_mae              REAL,
    holdout_mae_ensemble  REAL,
    feature_columns       TEXT    NOT NULL,
    model_path            TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mlruns_trained ON ml_runs(trained_at DESC);
"""

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
# Reads run on a per-thread connection to avoid cursor-state corruption
# when FastAPI's threadpool issues concurrent queries against the same
# sqlite3.Connection (which is not safe to share across threads even with
# check_same_thread=False).
_tls = threading.local()


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
            # feature_snapshots gained ml_max in the historical-backfill plan,
            # then blended_max once we started averaging the two predictors.
            feat_cols = {r["name"] for r in _conn.execute("PRAGMA table_info(feature_snapshots)").fetchall()}
            if "ml_max" not in feat_cols:
                _conn.execute("ALTER TABLE feature_snapshots ADD COLUMN ml_max REAL")
            if "blended_max" not in feat_cols:
                _conn.execute("ALTER TABLE feature_snapshots ADD COLUMN blended_max REAL")
            # historical_predictions gained gfs_mos_max + nam_mos_max
            hp_cols = {r["name"] for r in _conn.execute("PRAGMA table_info(historical_predictions)").fetchall()}
            for col in ("gfs_mos_max", "nam_mos_max"):
                if col not in hp_cols:
                    _conn.execute(f"ALTER TABLE historical_predictions ADD COLUMN {col} REAL")
            _conn.commit()


def _conn_or_init() -> sqlite3.Connection:
    """Per-thread sqlite3 connection. The first call also runs init() once
    so the schema/migrations are applied before any other thread queries."""
    if _conn is None:
        init()
    tls_conn = getattr(_tls, "conn", None)
    if tls_conn is None:
        tls_conn = _connect()
        _tls.conn = tls_conn
    return tls_conn


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
                rec_edge_cents, rec_kalshi_pct, rec_model_pct,
                ml_max, blended_max
            ) VALUES (?,?,?, ?,?,?,?,?, ?,?,?, ?,?,?,?,?,?, ?,?, ?,?, ?,?,?,?,?, ?,?,?, ?,?,?, ?,?)""",
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
                state.get("mlMax"),
                state.get("blendedMax"),
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


# ── Historical / ML ──────────────────────────────────────────────────────

def upsert_historical_prediction(row: Dict) -> None:
    """INSERT OR REPLACE one historical prediction row, but coalesce
    EVERY column against the existing row so a partial update doesn't
    wipe out fields the caller didn't include.

    This was a real bug source: a MOS-only fetch (gfs_mos_max +
    nam_mos_max set, everything else None) would land on top of an
    earlier Open-Meteo upsert (gfs_max / ecmwf_max / ensemble_max set)
    and the INSERT OR REPLACE blanked out the OM columns. After the
    full backfill, only ~22% of rows had a non-null ensemble_max."""
    c = _conn_or_init()
    coalesce_cols = (
        "gfs_max", "ecmwf_max", "icon_max", "om_max",
        "gfs_mos_max", "nam_mos_max",
        "t850_c", "t700_c", "t500_c", "h500_m", "rh850_pct",
        "ensemble_max",
    )
    with _lock:
        existing = c.execute(
            "SELECT * FROM historical_predictions "
            "WHERE city=? AND target_date=? AND forecast_horizon_hours=?",
            (row["city"], row["target_date"], int(row["forecast_horizon_hours"])),
        ).fetchone()
        merged = {col: row.get(col) for col in coalesce_cols}
        if existing:
            for col in coalesce_cols:
                if merged[col] is None and existing[col] is not None:
                    merged[col] = existing[col]
        # Don't create orphan rows: if there's no existing row AND we
        # don't have ensemble_max in the new data, skip the insert
        # entirely. (MOS-only inserts on dates that never had OM data
        # are useless for training and were the source of the orphan
        # bug.)
        if not existing and merged.get("ensemble_max") is None:
            return
        c.execute(
            """INSERT OR REPLACE INTO historical_predictions (
                city, target_date, forecast_horizon_hours,
                gfs_max, ecmwf_max, icon_max, om_max,
                gfs_mos_max, nam_mos_max,
                t850_c, t700_c, t500_c, h500_m, rh850_pct,
                ensemble_max
            ) VALUES (?,?,?, ?,?,?,?, ?,?, ?,?,?,?,?, ?)""",
            (
                row["city"], row["target_date"], int(row["forecast_horizon_hours"]),
                merged["gfs_max"], merged["ecmwf_max"],
                merged["icon_max"], merged["om_max"],
                merged["gfs_mos_max"], merged["nam_mos_max"],
                merged["t850_c"], merged["t700_c"], merged["t500_c"],
                merged["h500_m"], merged["rh850_pct"],
                merged["ensemble_max"],
            ),
        )
        c.commit()


def cleanup_orphan_predictions() -> int:
    """One-shot cleanup: delete historical_predictions rows that have
    NULL ensemble_max (MOS-only orphans from the upsert bug). Returns
    rows deleted. After running this, re-running backfill will refill
    the OM data correctly because the upsert now coalesces properly."""
    c = _conn_or_init()
    with _lock:
        before = c.execute("SELECT COUNT(*) AS n FROM historical_predictions").fetchone()["n"]
        c.execute("DELETE FROM historical_predictions WHERE ensemble_max IS NULL")
        c.commit()
        after = c.execute("SELECT COUNT(*) AS n FROM historical_predictions").fetchone()["n"]
    return int(before - after)


def upsert_historical_actual(city: str, target_date: str, actual_max_f: float, source: str) -> None:
    c = _conn_or_init()
    with _lock:
        c.execute(
            "INSERT OR REPLACE INTO historical_actuals (city, target_date, actual_max_f, source) VALUES (?,?,?,?)",
            (city, target_date, float(actual_max_f), source),
        )
        c.commit()


def historical_counts() -> Dict:
    """Quick stats for the UI: how much data we have to train on, plus
    feature coverage so we can diagnose why a model might be struggling
    (e.g., MOS sparse in older rows confuses mean-imputation)."""
    c = _conn_or_init()
    pred_n = c.execute("SELECT COUNT(*) AS n FROM historical_predictions").fetchone()["n"]
    act_n = c.execute("SELECT COUNT(*) AS n FROM historical_actuals").fetchone()["n"]
    paired = c.execute(
        """SELECT COUNT(*) AS n FROM historical_predictions p
           JOIN historical_actuals a USING (city, target_date)"""
    ).fetchone()["n"]
    earliest = c.execute(
        """SELECT MIN(target_date) AS d FROM historical_predictions p
           JOIN historical_actuals a USING (city, target_date)"""
    ).fetchone()["d"]
    latest = c.execute(
        """SELECT MAX(target_date) AS d FROM historical_predictions p
           JOIN historical_actuals a USING (city, target_date)"""
    ).fetchone()["d"]
    # Coverage % per major feature column — identifies which features are
    # sparse and may need a recency filter to avoid imputation-driven drift.
    coverage_row = c.execute(
        """SELECT
              COUNT(*)                                 AS total,
              SUM(CASE WHEN gfs_max IS NOT NULL THEN 1 ELSE 0 END)     AS gfs,
              SUM(CASE WHEN ecmwf_max IS NOT NULL THEN 1 ELSE 0 END)   AS ecmwf,
              SUM(CASE WHEN icon_max IS NOT NULL THEN 1 ELSE 0 END)    AS icon,
              SUM(CASE WHEN gfs_mos_max IS NOT NULL THEN 1 ELSE 0 END) AS gfs_mos,
              SUM(CASE WHEN nam_mos_max IS NOT NULL THEN 1 ELSE 0 END) AS nam_mos
           FROM historical_predictions"""
    ).fetchone()
    total = max(1, int(coverage_row["total"] or 0))
    coverage = {
        "gfs":     round(int(coverage_row["gfs"] or 0)     / total * 100, 1),
        "ecmwf":   round(int(coverage_row["ecmwf"] or 0)   / total * 100, 1),
        "icon":    round(int(coverage_row["icon"] or 0)    / total * 100, 1),
        "gfs_mos": round(int(coverage_row["gfs_mos"] or 0) / total * 100, 1),
        "nam_mos": round(int(coverage_row["nam_mos"] or 0) / total * 100, 1),
    }
    # Earliest date a feature has data — helps see "MOS only since YYYY-MM"
    earliest_mos = c.execute(
        "SELECT MIN(target_date) AS d FROM historical_predictions WHERE gfs_mos_max IS NOT NULL"
    ).fetchone()["d"]
    return {
        "predictions": pred_n,
        "actuals": act_n,
        "paired": paired,
        "earliest_target_date": earliest,
        "latest_target_date": latest,
        "feature_coverage_pct": coverage,
        "earliest_mos_date": earliest_mos,
    }


def list_training_data() -> List[Dict]:
    """Inner-join historical_predictions × historical_actuals for training.
    Returns one row per (city, target_date, forecast_horizon).

    Adds two derived columns that the trained model uses as features:
      - prev_actual_max_f: actual high from the previous day in this city
        (high day-to-day autocorrelation — biggest single signal we
        weren't using before)
      - seasonal_avg_max_f: long-run average actual max for this city ×
        month (climatology anchor)"""
    c = _conn_or_init()
    rows = c.execute(
        """
        WITH joined AS (
            SELECT p.city, p.target_date, p.forecast_horizon_hours,
                   p.gfs_max, p.ecmwf_max, p.icon_max, p.om_max,
                   p.gfs_mos_max, p.nam_mos_max,
                   p.t850_c, p.t700_c, p.t500_c, p.h500_m, p.rh850_pct,
                   p.ensemble_max,
                   a.actual_max_f
            FROM historical_predictions p
            JOIN historical_actuals a USING (city, target_date)
            WHERE p.ensemble_max IS NOT NULL  -- skip MOS-only / corrupted rows
        ),
        with_lag AS (
            SELECT j.*,
                   LAG(j.actual_max_f) OVER (
                       PARTITION BY j.city, j.forecast_horizon_hours
                       ORDER BY j.target_date
                   ) AS prev_actual_max_f,
                   substr(j.target_date, 6, 2) AS month_str
            FROM joined j
        ),
        climatology AS (
            SELECT city,
                   substr(target_date, 6, 2) AS month_str,
                   AVG(actual_max_f) AS seasonal_avg_max_f
            FROM historical_actuals
            GROUP BY city, substr(target_date, 6, 2)
        )
        SELECT w.city, w.target_date, w.forecast_horizon_hours,
               w.gfs_max, w.ecmwf_max, w.icon_max, w.om_max,
               w.gfs_mos_max, w.nam_mos_max,
               w.t850_c, w.t700_c, w.t500_c, w.h500_m, w.rh850_pct,
               w.ensemble_max,
               w.actual_max_f,
               w.prev_actual_max_f,
               c.seasonal_avg_max_f
        FROM with_lag w
        LEFT JOIN climatology c
          ON c.city = w.city AND c.month_str = w.month_str
        ORDER BY w.target_date ASC, w.city ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_prev_actual(city: str, target_date: str) -> Optional[float]:
    """Yesterday's actual high for `city` (the day before target_date).
    Used at inference time. Checks historical_actuals first, falls back
    to bets.settled_max_f if a settled bet is available."""
    from datetime import date, timedelta
    try:
        d = date.fromisoformat(target_date) - timedelta(days=1)
    except ValueError:
        return None
    prev = d.isoformat()
    c = _conn_or_init()
    row = c.execute(
        "SELECT actual_max_f FROM historical_actuals WHERE city = ? AND target_date = ?",
        (city, prev),
    ).fetchone()
    if row and row["actual_max_f"] is not None:
        return float(row["actual_max_f"])
    row = c.execute(
        "SELECT AVG(CAST(settled_max_f AS REAL)) AS m FROM bets "
        "WHERE city = ? AND target_date = ? AND settled_max_f IS NOT NULL",
        (city, prev),
    ).fetchone()
    if row and row["m"] is not None:
        return float(row["m"])
    return None


def get_seasonal_avg(city: str, target_date: str) -> Optional[float]:
    """Long-run climatology: average actual max for this city × month."""
    if not target_date or len(target_date) < 7:
        return None
    month = target_date[5:7]
    c = _conn_or_init()
    row = c.execute(
        "SELECT AVG(actual_max_f) AS m FROM historical_actuals "
        "WHERE city = ? AND substr(target_date, 6, 2) = ?",
        (city, month),
    ).fetchone()
    if row and row["m"] is not None:
        return float(row["m"])
    return None


def insert_ml_run(
    algorithm: str,
    n_train: int,
    n_test: int,
    train_mae: Optional[float],
    test_mae: Optional[float],
    holdout_mae_ensemble: Optional[float],
    feature_columns: List[str],
    model_path: str,
) -> Dict:
    import json, time as _time
    c = _conn_or_init()
    with _lock:
        cur = c.execute(
            """INSERT INTO ml_runs (
                trained_at, algorithm, n_train, n_test,
                train_mae, test_mae, holdout_mae_ensemble,
                feature_columns, model_path
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (int(_time.time()), algorithm, n_train, n_test,
             train_mae, test_mae, holdout_mae_ensemble,
             json.dumps(feature_columns), model_path),
        )
        c.commit()
        row = c.execute("SELECT * FROM ml_runs WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def latest_ml_run() -> Optional[Dict]:
    """Most recent ml_runs row whose pickle was actually persisted to
    disk (i.e. won an auto-sweep or was a single-algo train). Excludes
    the throw-away candidate rows we keep just for the history table."""
    c = _conn_or_init()
    row = c.execute(
        "SELECT * FROM ml_runs "
        "WHERE model_path IS NOT NULL AND model_path != '(not-persisted)' "
        "ORDER BY trained_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def list_ml_runs(limit: int = 20) -> List[Dict]:
    c = _conn_or_init()
    rows = c.execute(
        "SELECT id, trained_at, algorithm, n_train, n_test, train_mae, test_mae, holdout_mae_ensemble, model_path "
        "FROM ml_runs ORDER BY trained_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def _bucket_stats(errors: List[float]) -> Dict:
    n = len(errors)
    if n == 0:
        return {
            "n": 0, "mae": None, "median": None,
            "within_1_pct": None, "within_2_pct": None, "within_3_pct": None,
            "buckets": {"exact": None, "one_off": None, "two_off": None, "three_plus": None},
        }
    mae = sum(errors) / n
    med = sorted(errors)[n // 2]
    pct = lambda t: round(sum(1 for e in errors if e <= t) / n * 100, 1)
    # Mutually-exclusive buckets — these add up to 100%, unlike the
    # cumulative within_*_pct values. Maps directly to Kalshi 1°F-wide
    # brackets: exact = same bracket as actual, one_off = neighbor, etc.
    n_exact   = sum(1 for e in errors if e <= 1)
    n_one     = sum(1 for e in errors if 1 < e <= 2)
    n_two     = sum(1 for e in errors if 2 < e <= 3)
    n_three   = sum(1 for e in errors if e > 3)
    bp = lambda count: round(count / n * 100, 1)
    return {
        "n": n,
        "mae": round(mae, 2),
        "median": round(med, 2),
        "within_1_pct": pct(1),
        "within_2_pct": pct(2),
        "within_3_pct": pct(3),
        "buckets": {
            "exact":      bp(n_exact),    # ≤1°F — same Kalshi bracket
            "one_off":    bp(n_one),      # 1-2°F — one bracket off
            "two_off":    bp(n_two),      # 2-3°F — two off
            "three_plus": bp(n_three),    # >3°F — far miss
        },
    }


def accuracy_summary() -> Dict:
    """Pair the latest model_max, ml_max, AND blended_max per (city,
    target_date) with the actual high observed for that day and roll up
    overall + per-city accuracy stats. Returns three parallel blocks
    (ensemble / ml / blended) so the UI can render head-to-head-to-head."""
    c = _conn_or_init()
    rows = c.execute(
        """
        WITH latest_pred AS (
            SELECT city, target_date, model_max, ml_max, blended_max,
                   ROW_NUMBER() OVER (
                       PARTITION BY city, target_date
                       ORDER BY ts DESC
                   ) AS rn
            FROM feature_snapshots
            WHERE target_date IS NOT NULL AND model_max IS NOT NULL
        ),
        actuals AS (
            SELECT city, target_date,
                   AVG(CAST(settled_max_f AS REAL)) AS actual_max
            FROM bets
            WHERE status = 'settled'
              AND settled_max_f IS NOT NULL
              AND target_date IS NOT NULL
            GROUP BY city, target_date
        )
        SELECT p.city, p.target_date, p.model_max, p.ml_max, p.blended_max, a.actual_max
        FROM latest_pred p
        JOIN actuals a ON a.city = p.city AND a.target_date = p.target_date
        WHERE p.rn = 1
        ORDER BY p.target_date DESC, p.city ASC
        """
    ).fetchall()
    pairs = [dict(r) for r in rows]

    ens_errors = [abs(p["model_max"] - p["actual_max"]) for p in pairs]
    ml_errors = [abs(p["ml_max"] - p["actual_max"])
                 for p in pairs if p["ml_max"] is not None]
    blend_errors = [abs(p["blended_max"] - p["actual_max"])
                    for p in pairs if p["blended_max"] is not None]

    by_city: Dict[str, Dict[str, List[float]]] = {}
    for p in pairs:
        d = by_city.setdefault(p["city"], {"ens": [], "ml": [], "blend": []})
        d["ens"].append(abs(p["model_max"] - p["actual_max"]))
        if p["ml_max"] is not None:
            d["ml"].append(abs(p["ml_max"] - p["actual_max"]))
        if p["blended_max"] is not None:
            d["blend"].append(abs(p["blended_max"] - p["actual_max"]))

    return {
        "n_predictions": len(pairs),
        "ensemble": _bucket_stats(ens_errors),
        "ml": _bucket_stats(ml_errors),
        "blended": _bucket_stats(blend_errors),
        "by_city": [
            {
                "city": city,
                "n": len(d["ens"]),
                "ensemble_mae": round(sum(d["ens"]) / len(d["ens"]), 2) if d["ens"] else None,
                "ml_mae": round(sum(d["ml"]) / len(d["ml"]), 2) if d["ml"] else None,
                "blend_mae": round(sum(d["blend"]) / len(d["blend"]), 2) if d["blend"] else None,
                "ml_n": len(d["ml"]),
                "blend_n": len(d["blend"]),
            }
            for city, d in sorted(by_city.items())
        ],
        "recent": [
            {
                "city": p["city"],
                "date": p["target_date"],
                "ensemble": round(p["model_max"], 1),
                "ml": round(p["ml_max"], 1) if p["ml_max"] is not None else None,
                "blended": round(p["blended_max"], 1) if p["blended_max"] is not None else None,
                "actual": round(p["actual_max"], 1),
                "ensemble_error": round(p["model_max"] - p["actual_max"], 2),
                "ml_error": round(p["ml_max"] - p["actual_max"], 2) if p["ml_max"] is not None else None,
                "blend_error": round(p["blended_max"] - p["actual_max"], 2) if p["blended_max"] is not None else None,
            }
            for p in pairs[:30]
        ],
    }


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
