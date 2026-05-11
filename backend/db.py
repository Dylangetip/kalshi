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

import json
import sqlite3
import threading
import time
import time as _time  # alias for legacy in-function imports below
from pathlib import Path
from datetime import datetime, timedelta
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

-- Activity log surfacing what the ML pipeline is doing in real time.
-- Powers the "what's it actively learning / changing" feed on the ML tab.
-- Kinds: backfill_start | backfill_done | retrain_start | retrain_done
--      | model_swap | drift_alert | prune | data_quality | dist_shift
--      | model_promoted | model_rolled_back | online_update
CREATE TABLE IF NOT EXISTS ml_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    kind      TEXT    NOT NULL,
    payload   TEXT    NOT NULL,   -- JSON
    message   TEXT    NOT NULL    -- human-readable line
);
CREATE INDEX IF NOT EXISTS idx_mlevents_ts ON ml_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_mlevents_kind ON ml_events(kind, ts DESC);

-- Per-(run, city) test residual breakdown so we can spot which cities
-- the model is dragging on. Populated after every train() that has a
-- usable test split. bias = mean(predicted - actual) for the city.
CREATE TABLE IF NOT EXISTS ml_city_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES ml_runs(id),
    city        TEXT    NOT NULL,
    test_mae    REAL,
    bias        REAL,
    n_samples   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mlcm_run  ON ml_city_metrics(run_id);
CREATE INDEX IF NOT EXISTS idx_mlcm_city ON ml_city_metrics(city);

-- Synthetic ML predictions backfilled from historical_predictions ×
-- historical_actuals via the live ML model. Bumps the ML-bias map's
-- effective sample size from 5 days of real snapshots to whatever
-- coverage historical_predictions has (typically 1-2 years × 19 cities).
-- compute_ml_city_bias UNIONs this with feature_snapshots so corrections
-- stabilize quickly without waiting for natural data accrual.
CREATE TABLE IF NOT EXISTS ml_historical_predictions (
    city                   TEXT    NOT NULL,
    target_date            TEXT    NOT NULL,
    forecast_horizon_hours INTEGER NOT NULL DEFAULT 24,
    ml_max                 REAL    NOT NULL,
    replayed_at            INTEGER NOT NULL,
    PRIMARY KEY (city, target_date, forecast_horizon_hours)
);
CREATE INDEX IF NOT EXISTS idx_mlhp_target ON ml_historical_predictions(target_date);

-- Virtual account ledger. The auto-trader runs against a private bank
-- account: deposits/withdrawals come from the user, bets debit the stake
-- at placement, winning settlements credit (stake + profit), losing
-- settlements record a zero-amount row for the audit trail. balance_after
-- is the running balance AFTER this transaction so the chart can plot
-- it directly without recomputing the prefix sum on every read.
CREATE TABLE IF NOT EXISTS account_transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    INTEGER NOT NULL,
    type          TEXT    NOT NULL CHECK(type IN ('deposit','withdrawal','bet_placed','bet_won','bet_lost')),
    amount        REAL    NOT NULL,
    balance_after REAL    NOT NULL,
    note          TEXT,
    bet_id        INTEGER REFERENCES bets(id)
);
CREATE INDEX IF NOT EXISTS idx_acct_tx_created ON account_transactions(created_at);
CREATE INDEX IF NOT EXISTS idx_acct_tx_bet     ON account_transactions(bet_id);
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
            # ml_runs gained role (champion/challenger/archived) + hyperparams JSON
            mlrun_cols = {r["name"] for r in _conn.execute("PRAGMA table_info(ml_runs)").fetchall()}
            if "role" not in mlrun_cols:
                _conn.execute("ALTER TABLE ml_runs ADD COLUMN role TEXT DEFAULT 'champion'")
            if "hyperparams" not in mlrun_cols:
                _conn.execute("ALTER TABLE ml_runs ADD COLUMN hyperparams TEXT")
            if "walk_forward_mae" not in mlrun_cols:
                _conn.execute("ALTER TABLE ml_runs ADD COLUMN walk_forward_mae REAL")
            # Fingerprint = stable hash of (algorithm + n_train + features +
            # hyperparams). Lets us skip a retrain when nothing changed.
            if "fingerprint" not in mlrun_cols:
                _conn.execute("ALTER TABLE ml_runs ADD COLUMN fingerprint TEXT")
                _conn.execute("CREATE INDEX IF NOT EXISTS idx_mlruns_fp ON ml_runs(fingerprint)")
            if "skipped" not in mlrun_cols:
                _conn.execute("ALTER TABLE ml_runs ADD COLUMN skipped INTEGER DEFAULT 0")
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


def delete_bet(bet_id: int) -> int:
    """Hard-delete a bet row. Used as a rollback when downstream side
    effects (e.g. account ledger debit) fail after the bet is inserted —
    keeps the bets table and the ledger consistent. Returns rowcount."""
    c = _conn_or_init()
    with _lock:
        cur = c.execute("DELETE FROM bets WHERE id = ?", (int(bet_id),))
        c.commit()
    return int(cur.rowcount)


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


def list_settled_bets() -> List[Dict]:
    """All-time settled bets, newest settlement first."""
    c = _conn_or_init()
    rows = c.execute(
        "SELECT * FROM bets WHERE status='settled' "
        "ORDER BY COALESCE(settled_at, placed_at/1000) DESC"
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
    """Persist a city snapshot. The model_max column records the ACTIVE
    prediction — the value that actually drives the ladder, recommended
    bracket, and edge calc. When an ML model is loaded that's the ML
    output (or blend); when not, it falls back to the raw ensemble. The
    raw ensemble feature is still recorded in feature_snapshots.model_max
    for downstream training/bias use."""
    if ts is None:
        ts = int(time.time())
    best = state["settlementBracket"]
    active = state.get("activeMax")
    if active is None:
        active = state["modelMax"]
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
                float(active),
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


def recompute_historical_ensemble_max() -> Dict[str, int]:
    """Recompute every historical_predictions row's ensemble_max using the
    LIVE blend formula (model.live_style_ensemble) so training data uses
    the same ensemble pipeline that drives live inference.

    Before: ensemble_max was a 3-way (gfs / ecmwf / icon) Open-Meteo blend
    written by backfill._row_from_open_meteo. Live ensemble_max was a 5-way
    MOS/NAM/NWS/OM/ECMWF blend with bias correction. The ML model trained
    on column A and inferred on column B — covariate shift baked in.

    After: both paths route through live_style_ensemble(). NWS daily-forecast
    isn't archived historically, so historical rows pass None for that slot
    and ensemble_model_max renormalizes weights across the four available
    sources.

    Rows whose four sources are ALL null get ensemble_max cleared to NULL
    (consistent with cleanup_orphan_predictions). Returns a counts dict so
    the admin endpoint can show before/after parity.
    """
    from .model import live_style_ensemble
    c = _conn_or_init()
    updated = 0
    cleared = 0
    skipped = 0
    with _lock:
        rows = c.execute(
            """SELECT city, target_date, forecast_horizon_hours,
                      gfs_mos_max, nam_mos_max, om_max, ecmwf_max,
                      ensemble_max
                 FROM historical_predictions""",
        ).fetchall()
        for r in rows:
            new_val = live_style_ensemble(
                r["gfs_mos_max"], r["nam_mos_max"],
                None,  # NWS daily-forecast not archived for historical dates
                r["om_max"], r["ecmwf_max"],
            )
            old_val = r["ensemble_max"]
            if new_val is None and old_val is None:
                skipped += 1
                continue
            new_rounded = round(new_val, 2) if new_val is not None else None
            if new_rounded == (round(old_val, 2) if old_val is not None else None):
                skipped += 1
                continue
            c.execute(
                """UPDATE historical_predictions
                      SET ensemble_max = ?
                    WHERE city = ?
                      AND target_date = ?
                      AND forecast_horizon_hours = ?""",
                (
                    new_rounded,
                    r["city"], r["target_date"], int(r["forecast_horizon_hours"]),
                ),
            )
            if new_rounded is None:
                cleared += 1
            else:
                updated += 1
        c.commit()
    return {"updated": updated, "cleared": cleared, "skipped": skipped, "total": len(rows)}


# ── Virtual account ledger ─────────────────────────────────────────────

class InsufficientFundsError(ValueError):
    """Raised when a debit (withdrawal or stake) would push the account
    below the minimum allowed balance. Caller should surface as 4xx."""


_VALID_TX_TYPES = ("deposit", "withdrawal", "bet_placed", "bet_won", "bet_lost")


def account_balance() -> float:
    """Authoritative balance: SUM(amount) over the ledger. The persisted
    config value mirrors this and the startup integrity check enforces
    they agree."""
    c = _conn_or_init()
    row = c.execute(
        "SELECT COALESCE(SUM(amount), 0.0) AS bal FROM account_transactions"
    ).fetchone()
    return float(row["bal"] or 0.0)


def _record_account_transaction(
    tx_type: str,
    amount: float,
    note: Optional[str] = None,
    bet_id: Optional[int] = None,
    min_balance: float = 0.0,
    allow_negative: bool = False,
) -> Dict:
    """Single-writer entry point for the account ledger. Recomputes the
    running balance under the lock, refuses any debit that would drop
    below `min_balance` (unless allow_negative=True for internal admin
    flows), and writes one row with the post-transaction balance baked
    in so reads don't have to recompute the prefix sum.

    Returns the inserted row as a dict."""
    if tx_type not in _VALID_TX_TYPES:
        raise ValueError(f"invalid tx_type {tx_type!r}; expected one of {_VALID_TX_TYPES}")
    c = _conn_or_init()
    with _lock:
        cur_bal_row = c.execute(
            "SELECT COALESCE(SUM(amount), 0.0) AS bal FROM account_transactions"
        ).fetchone()
        cur_bal = float(cur_bal_row["bal"] or 0.0)
        new_bal = round(cur_bal + float(amount), 2)
        if not allow_negative and new_bal < min_balance - 1e-6:
            raise InsufficientFundsError(
                f"transaction would leave balance ${new_bal:.2f} below "
                f"minimum ${min_balance:.2f} (current ${cur_bal:.2f}, "
                f"requested ${amount:+.2f})"
            )
        ts = int(time.time())
        cur = c.execute(
            """INSERT INTO account_transactions
                 (created_at, type, amount, balance_after, note, bet_id)
                 VALUES (?,?,?,?,?,?)""",
            (ts, tx_type, float(amount), new_bal, note, bet_id),
        )
        c.commit()
        row = c.execute(
            "SELECT * FROM account_transactions WHERE id = ?", (cur.lastrowid,),
        ).fetchone()
    return dict(row)


def account_deposit(amount: float, note: Optional[str] = None) -> Dict:
    if amount <= 0:
        raise ValueError(f"deposit amount must be > 0 (got {amount})")
    return _record_account_transaction("deposit", float(amount), note=note)


def account_withdraw(amount: float, note: Optional[str] = None) -> Dict:
    """Withdraw `amount` (positive). Refused if it would leave a negative
    balance — the spec calls for >= $0 floor on user-initiated withdrawals."""
    if amount <= 0:
        raise ValueError(f"withdrawal amount must be > 0 (got {amount})")
    return _record_account_transaction(
        "withdrawal", -float(amount), note=note, min_balance=0.0,
    )


def account_record_bet_placed(bet_id: int, stake: float, min_balance: float = 0.0) -> Dict:
    """Debit the stake at bet placement. Raises InsufficientFundsError if
    the debit would breach `min_balance`. Caller should NOT also try to
    insert the bet row first if this might fail — wrap the bet insert and
    this call in a guard so we don't end up with a placed bet without a
    matching ledger entry."""
    if stake <= 0:
        raise ValueError(f"stake must be > 0 (got {stake})")
    return _record_account_transaction(
        "bet_placed", -float(stake), bet_id=int(bet_id), min_balance=float(min_balance),
    )


def account_record_bet_won(bet_id: int, gross_payout: float) -> Dict:
    """Credit the gross payout (stake + profit) at settlement. Allowed
    even if it pushes the balance up (obviously) — credit-only path."""
    if gross_payout <= 0:
        raise ValueError(f"gross_payout must be > 0 (got {gross_payout})")
    return _record_account_transaction(
        "bet_won", float(gross_payout), bet_id=int(bet_id), allow_negative=True,
    )


def account_record_bet_lost(bet_id: int) -> Dict:
    """Audit-only zero-amount entry. Stake was already debited at
    placement, so a loss has no balance impact — but the row gives the
    transaction log a complete history per bet_id."""
    return _record_account_transaction(
        "bet_lost", 0.0, bet_id=int(bet_id), allow_negative=True,
    )


def list_account_transactions(limit: int = 50) -> List[Dict]:
    c = _conn_or_init()
    rows = c.execute(
        "SELECT * FROM account_transactions ORDER BY created_at DESC, id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def account_balance_history() -> List[Dict]:
    """Time-ordered (created_at, balance_after) sequence for the chart.
    Smaller payload than list_account_transactions; ordering is ascending
    so the frontend can plot directly."""
    c = _conn_or_init()
    rows = c.execute(
        """SELECT created_at, balance_after, type
             FROM account_transactions
            ORDER BY created_at ASC, id ASC""",
    ).fetchall()
    return [dict(r) for r in rows]


def account_bet_was_funded(bet_id: int) -> bool:
    """True if a bet_placed row exists for this bet_id — i.e. the bet was
    placed under account_mode and its settlement should hit the ledger.
    Settlements of bets placed before account_mode was enabled are silently
    skipped so we don't credit phantom money to the virtual account."""
    c = _conn_or_init()
    row = c.execute(
        "SELECT 1 FROM account_transactions WHERE bet_id = ? AND type = 'bet_placed' LIMIT 1",
        (int(bet_id),),
    ).fetchone()
    return row is not None


def account_transaction_count() -> int:
    c = _conn_or_init()
    row = c.execute("SELECT COUNT(*) AS n FROM account_transactions").fetchone()
    return int(row["n"] or 0)


def upsert_historical_actual(city: str, target_date: str, actual_max_f: float, source: str) -> None:
    c = _conn_or_init()
    with _lock:
        c.execute(
            "INSERT OR REPLACE INTO historical_actuals (city, target_date, actual_max_f, source) VALUES (?,?,?,?)",
            (city, target_date, float(actual_max_f), source),
        )
        c.commit()


def get_historical_actual(city: str, target_date: str) -> Optional[Dict]:
    c = _conn_or_init()
    row = c.execute(
        "SELECT city, target_date, actual_max_f, source FROM historical_actuals "
        "WHERE city = ? AND target_date = ? LIMIT 1",
        (city, target_date),
    ).fetchone()
    return dict(row) if row else None


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
                   LAG(j.actual_max_f, 1) OVER (
                       PARTITION BY j.city, j.forecast_horizon_hours
                       ORDER BY j.target_date
                   ) AS prev_actual_max_f,
                   LAG(j.actual_max_f, 3) OVER (
                       PARTITION BY j.city, j.forecast_horizon_hours
                       ORDER BY j.target_date
                   ) AS lag3_actual_max_f,
                   LAG(j.actual_max_f, 7) OVER (
                       PARTITION BY j.city, j.forecast_horizon_hours
                       ORDER BY j.target_date
                   ) AS lag7_actual_max_f,
                   AVG(j.actual_max_f) OVER (
                       PARTITION BY j.city
                       ORDER BY j.target_date
                       ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
                   ) AS roll7_mean_max_f,
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
               w.lag3_actual_max_f,
               w.lag7_actual_max_f,
               w.roll7_mean_max_f,
               c.seasonal_avg_max_f
        FROM with_lag w
        LEFT JOIN climatology c
          ON c.city = w.city AND c.month_str = w.month_str
        ORDER BY w.target_date ASC, w.city ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def list_snapshot_training_data() -> List[Dict]:
    """Snapshot-derived training rows. Each feature_snapshot row joined
    with the eventual NWS actual for that target_date, surfaced with a
    real per-row forecast_horizon_hours computed from snapshot ts vs the
    target date midnight. Multiplies the available training data ~50-200×
    over historical_predictions alone (one snap per ~5min vs one daily
    archive row per day) AND injects real horizon variance the daily
    archive can't provide.

    Output shape mirrors list_training_data() so the trainer can cat the
    two sources together and use the same featurization. Lag/seasonal
    fields are reused from the same window functions over actuals.
    """
    c = _conn_or_init()
    rows = c.execute(
        """
        WITH joined AS (
            SELECT fs.city,
                   fs.target_date,
                   CAST(MAX(0,
                       (julianday(fs.target_date) - (fs.ts / 86400.0 + 2440587.5)) * 24.0
                   ) AS INTEGER) AS forecast_horizon_hours,
                   -- feature_snapshots doesn't store raw GFS/ICON; use
                   -- MOS as the GFS proxy (correlated) and NULL for icon
                   -- so the imputer fills with column mean. The richer
                   -- snapshot columns (mos, ecmwf, om, upper-air) carry
                   -- most of the signal anyway.
                   fs.gfs_mos_max AS gfs_max,
                   fs.ecmwf_max,
                   NULL           AS icon_max,
                   fs.om_max,
                   fs.gfs_mos_max,
                   fs.nam_mos_max,
                   fs.t850_c, fs.t700_c, fs.t500_c, fs.h500_m, fs.rh850_pct,
                   fs.model_max AS ensemble_max,
                   a.actual_max_f
            FROM feature_snapshots fs
            JOIN historical_actuals a USING (city, target_date)
            WHERE fs.model_max IS NOT NULL
        ),
        with_lag AS (
            SELECT j.*,
                   LAG(j.actual_max_f, 1) OVER (
                       PARTITION BY j.city
                       ORDER BY j.target_date, j.forecast_horizon_hours
                   ) AS prev_actual_max_f,
                   LAG(j.actual_max_f, 3) OVER (
                       PARTITION BY j.city
                       ORDER BY j.target_date, j.forecast_horizon_hours
                   ) AS lag3_actual_max_f,
                   LAG(j.actual_max_f, 7) OVER (
                       PARTITION BY j.city
                       ORDER BY j.target_date, j.forecast_horizon_hours
                   ) AS lag7_actual_max_f,
                   AVG(j.actual_max_f) OVER (
                       PARTITION BY j.city
                       ORDER BY j.target_date
                       ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
                   ) AS roll7_mean_max_f,
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
               w.lag3_actual_max_f,
               w.lag7_actual_max_f,
               w.roll7_mean_max_f,
               c.seasonal_avg_max_f
        FROM with_lag w
        LEFT JOIN climatology c
          ON c.city = w.city AND c.month_str = w.month_str
        ORDER BY w.target_date ASC, w.forecast_horizon_hours ASC, w.city ASC
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


def get_lag_actual(city: str, target_date: str, days: int) -> Optional[float]:
    """Actual high `days` days before target_date. Powers lag3/lag7
    feature lookups at inference time."""
    from datetime import date, timedelta
    try:
        d = date.fromisoformat(target_date) - timedelta(days=days)
    except ValueError:
        return None
    c = _conn_or_init()
    row = c.execute(
        "SELECT actual_max_f FROM historical_actuals WHERE city = ? AND target_date = ?",
        (city, d.isoformat()),
    ).fetchone()
    if row and row["actual_max_f"] is not None:
        return float(row["actual_max_f"])
    return None


def get_rolling_mean_actual(city: str, target_date: str, days: int = 7) -> Optional[float]:
    """Rolling mean of actual highs over the `days` days before target_date."""
    from datetime import date, timedelta
    try:
        end = date.fromisoformat(target_date) - timedelta(days=1)
    except ValueError:
        return None
    start = end - timedelta(days=days - 1)
    c = _conn_or_init()
    row = c.execute(
        "SELECT AVG(actual_max_f) AS m FROM historical_actuals "
        "WHERE city = ? AND target_date >= ? AND target_date <= ?",
        (city, start.isoformat(), end.isoformat()),
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


def compute_ml_run_fingerprint(
    algorithm: str,
    n_train: int,
    feature_columns: List[str],
    hyperparams: Optional[Dict] = None,
) -> str:
    """Stable hash that identifies a "would produce the same model" config.
    Skipping a retrain when the fingerprint matches an already-recorded row
    keeps ml_runs from filling up with identical no-op rows."""
    import hashlib
    payload = json.dumps({
        "algorithm": algorithm,
        "n_train": int(n_train or 0),
        "features": sorted([f for f in (feature_columns or []) if f]),
        "hp": hyperparams or {},
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def find_ml_run_by_fingerprint(fingerprint: str) -> Optional[Dict]:
    """Return the most-recent ml_runs row matching a fingerprint, or None.
    Used by the retrain loop to short-circuit when nothing has changed."""
    if not fingerprint:
        return None
    c = _conn_or_init()
    row = c.execute(
        "SELECT * FROM ml_runs WHERE fingerprint = ? "
        "AND COALESCE(skipped, 0) = 0 "
        "ORDER BY trained_at DESC, id DESC LIMIT 1",
        (fingerprint,),
    ).fetchone()
    return dict(row) if row else None


def log_ml_run_skipped(
    algorithm: str,
    n_train: int,
    fingerprint: str,
    matched_run_id: int,
) -> Dict:
    """Insert a stub row marking that we skipped a retrain because the
    fingerprint already exists. Lets the activity feed show 'skipped'
    cleanly without polluting the metric history."""
    c = _conn_or_init()
    with _lock:
        cur = c.execute(
            """INSERT INTO ml_runs (
                trained_at, algorithm, n_train, n_test,
                feature_columns, model_path,
                fingerprint, skipped, role
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (int(_time.time()), algorithm, n_train, 0,
             json.dumps([]), f"(skipped, see run {matched_run_id})",
             fingerprint, 1, "archived"),
        )
        c.commit()
        row = c.execute("SELECT * FROM ml_runs WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def insert_ml_run(
    algorithm: str,
    n_train: int,
    n_test: int,
    train_mae: Optional[float],
    test_mae: Optional[float],
    holdout_mae_ensemble: Optional[float],
    feature_columns: List[str],
    model_path: str,
    walk_forward_mae: Optional[float] = None,
    hyperparams: Optional[Dict] = None,
    role: str = "champion",
    fingerprint: Optional[str] = None,
) -> Dict:
    if fingerprint is None:
        fingerprint = compute_ml_run_fingerprint(
            algorithm, n_train, feature_columns, hyperparams,
        )
    c = _conn_or_init()
    with _lock:
        cur = c.execute(
            """INSERT INTO ml_runs (
                trained_at, algorithm, n_train, n_test,
                train_mae, test_mae, holdout_mae_ensemble,
                feature_columns, model_path,
                walk_forward_mae, hyperparams, role, fingerprint
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(_time.time()), algorithm, n_train, n_test,
             train_mae, test_mae, holdout_mae_ensemble,
             json.dumps(feature_columns), model_path,
             walk_forward_mae,
             json.dumps(hyperparams) if hyperparams else None,
             role, fingerprint),
        )
        c.commit()
        row = c.execute("SELECT * FROM ml_runs WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def insert_ml_city_metrics(run_id: int, rows: List[Dict]) -> int:
    """Bulk-insert per-(city) test residuals for a training run.
    rows: [{city, test_mae, bias, n_samples}, ...]"""
    if not rows:
        return 0
    c = _conn_or_init()
    with _lock:
        c.executemany(
            "INSERT INTO ml_city_metrics (run_id, city, test_mae, bias, n_samples) "
            "VALUES (?, ?, ?, ?, ?)",
            [(run_id, r["city"], r.get("test_mae"), r.get("bias"), r.get("n_samples", 0))
             for r in rows],
        )
        c.commit()
    return len(rows)


def list_ml_city_metrics(run_id: Optional[int] = None, limit: int = 100) -> List[Dict]:
    c = _conn_or_init()
    if run_id is None:
        rows = c.execute(
            "SELECT * FROM ml_city_metrics ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM ml_city_metrics WHERE run_id = ? ORDER BY city",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


_city_mae_cache: Dict = {"ts": 0, "map": None, "global_mae": None}


def get_city_mae_map(ttl_seconds: int = 300) -> Dict[str, float]:
    """Most-recent test_mae per city, used by the auto-trader to scale
    bet sizing / skip cities the model is bad at.

    Strategy: pull the most-recent ml_city_metrics row per city across
    ALL runs (champion or otherwise). The sweep loop records city
    metrics on every candidate, so this stays fresh; even if no
    metrics exist for the current champion, the previous run's metrics
    are still informative.

    Cached for ttl_seconds — invalidated automatically when a new
    champion gets promoted (see _on_champion_change calls). Lookup is
    keyed by city CODE (NYC, LAX, …)."""
    now = int(_time.time())
    if _city_mae_cache["map"] is not None and (now - _city_mae_cache["ts"]) < ttl_seconds:
        return dict(_city_mae_cache["map"])
    c = _conn_or_init()
    rows = c.execute(
        """SELECT city, test_mae
             FROM ml_city_metrics
            WHERE id IN (SELECT MAX(id) FROM ml_city_metrics GROUP BY city)
              AND test_mae IS NOT NULL""",
    ).fetchall()
    out = {r["city"]: float(r["test_mae"]) for r in rows if r["city"]}
    _city_mae_cache.update({"ts": now, "map": out})
    return dict(out)


def invalidate_city_mae_cache() -> None:
    """Reset the city-MAE cache. Call after a new champion is promoted so
    the auto-trader picks up the freshest per-city stats on the next tick."""
    _city_mae_cache.update({"ts": 0, "map": None})


def list_runs_by_role(role: str, limit: int = 10) -> List[Dict]:
    """Filter ml_runs by role (champion / challenger / archived)."""
    c = _conn_or_init()
    rows = c.execute(
        "SELECT * FROM ml_runs WHERE role = ? AND model_path != '(not-persisted)' "
        "ORDER BY trained_at DESC LIMIT ?",
        (role, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def update_ml_run_role(run_id: int, role: str) -> None:
    """Promote / demote a single ml_runs row. Used by champion/challenger
    logic to track which model is live."""
    c = _conn_or_init()
    with _lock:
        c.execute("UPDATE ml_runs SET role = ? WHERE id = ?", (role, run_id))
        c.commit()


def archive_other_champions(except_id: int) -> int:
    """Archive every persisted-as-champion row OTHER than `except_id`.
    Sweeping fix for the historical drift where every algo in every
    sweep was being marked champion. Returns the number archived."""
    c = _conn_or_init()
    with _lock:
        cur = c.execute(
            "UPDATE ml_runs SET role='archived' "
            "WHERE role='champion' AND id != ? "
            "AND COALESCE(skipped, 0) = 0",
            (except_id,),
        )
        archived = cur.rowcount
        c.commit()
    return archived


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
        "SELECT id, trained_at, algorithm, n_train, n_test, train_mae, test_mae, "
        "holdout_mae_ensemble, model_path, role, walk_forward_mae "
        "FROM ml_runs ORDER BY trained_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ───── ML activity events ─────────────────────────────────────────────────
# Powers the "what's it actively learning + changing" feed on the ML tab.
# Every meaningful pipeline event (backfill, retrain, model swap, drift
# alert, etc) lands in ml_events with a JSON payload + a one-line message
# the UI can render verbatim.

def log_ml_event(kind: str, payload: Optional[Dict] = None, message: str = "") -> int:
    """Append a single event to the ml_events log. Idempotent; safe to
    call from any background task. Never raises — logging shouldn't take
    down a training run."""
    try:
        c = _conn_or_init()
        with _lock:
            cur = c.execute(
                "INSERT INTO ml_events (ts, kind, payload, message) VALUES (?,?,?,?)",
                (int(_time.time()), kind, json.dumps(payload or {}), message or kind),
            )
            c.commit()
            return cur.lastrowid
    except Exception as exc:  # noqa: BLE001
        # ml_events not critical — fall through silently so a logging
        # failure can't block the actual training/backfill work.
        print(f"[db] log_ml_event swallowed error: {exc}")
        return 0


def list_ml_events(limit: int = 200, since_ts: Optional[int] = None,
                   kinds: Optional[List[str]] = None) -> List[Dict]:
    """Newest-first list of recent ml_events. payload is parsed JSON;
    callers can filter by since_ts for cheap polling."""
    c = _conn_or_init()
    sql = "SELECT id, ts, kind, payload, message FROM ml_events"
    params: List = []
    where = []
    if since_ts is not None:
        where.append("ts >= ?")
        params.append(int(since_ts))
    if kinds:
        placeholders = ",".join("?" * len(kinds))
        where.append(f"kind IN ({placeholders})")
        params.extend(kinds)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(int(limit))
    rows = c.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
        except (TypeError, ValueError):
            d["payload"] = {}
        out.append(d)
    return out


def latest_event(kind: str) -> Optional[Dict]:
    """Most-recent event of a given kind, or None. Used to compute
    'currently training/backfilling' status — a 'retrain_start' newer
    than the matching 'retrain_done' means a run is in flight."""
    c = _conn_or_init()
    row = c.execute(
        "SELECT id, ts, kind, payload, message FROM ml_events "
        "WHERE kind = ? ORDER BY ts DESC, id DESC LIMIT 1",
        (kind,),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
    except (TypeError, ValueError):
        d["payload"] = {}
    return d


def count_paired_total() -> int:
    """Total number of joined (city, target_date) pairs available for
    training. Drives the data-threshold retrain trigger by comparing
    against a stored count from the last successful retrain."""
    c = _conn_or_init()
    row = c.execute(
        """SELECT COUNT(*) AS n
           FROM historical_predictions p
           JOIN historical_actuals a USING (city, target_date)
           WHERE p.ensemble_max IS NOT NULL""",
    ).fetchone()
    return int(row["n"]) if row else 0


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
    /api/stats for the P&L view to swap in real win-rate.

    Also computes the rolling bankroll the auto-trader treats as a bank
    account:
      starting_cash + sum(settled_pl) − sum(open bet stakes)
    Money leaves the account when a bet is placed (stake committed),
    returns + profit when it wins, and stays gone when it loses.
    """
    c = _conn_or_init()
    row = c.execute(
        """SELECT
              COUNT(*) FILTER (WHERE status='settled')                       AS settled,
              COUNT(*) FILTER (WHERE status='settled' AND settled_pl > 0)    AS won,
              COUNT(*) FILTER (WHERE status='open')                          AS open,
              COALESCE(SUM(settled_pl) FILTER (WHERE status='settled'), 0.0) AS realized_pl,
              COALESCE(SUM(size) FILTER (WHERE status='open'), 0)            AS open_stakes
           FROM bets"""
    ).fetchone()
    settled = int(row["settled"])
    realized_pl = float(row["realized_pl"])
    open_stakes = float(row["open_stakes"])
    return {
        "settled": settled,
        "won": int(row["won"]),
        "open": int(row["open"]),
        "winRate": (float(row["won"]) / settled) if settled else None,
        "realizedPl": round(realized_pl, 2),
        "openStakes": round(open_stakes, 2),
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


def compute_city_bias(half_life_days: float = 30.0, lookback_days: int = 180) -> Dict:
    """Per-(city, horizon-bucket) bias of the ensemble forecast vs the
    NWS-published actual. Positive value → model has been UNDER-forecasting
    (actuals are warmer than predictions) → caller adds the value to the
    raw model_max to correct.

    Uses an exponentially-decaying weight on each historical day so recent
    misses dominate older ones. Half-life default is 30 days, so a residual
    from 30 days ago carries half the weight of today's.

    Horizon buckets:
      "short" — same-day / next-morning forecast (≤24h to target)
      "mid"   — 24–72h forecast (1–3 days ahead)
      "long"  — >72h forecast

    Returns:
      {
        (city, bucket): {
          "mean_residual": float,   # actual − model, +F
          "n_eff": float,           # sum of weights (effective sample size)
          "n_raw": int,             # raw count of joined rows
        },
        ...
      }
    """
    import math
    c = _conn_or_init()
    today = datetime.utcnow().date().isoformat()
    cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).date().isoformat()
    rows = c.execute(
        """
        SELECT hp.city,
               hp.target_date,
               hp.forecast_horizon_hours AS horizon,
               hp.ensemble_max,
               ha.actual_max_f,
               julianday(?) - julianday(hp.target_date) AS days_old
        FROM historical_predictions hp
        JOIN historical_actuals     ha
          ON ha.city = hp.city AND ha.target_date = hp.target_date
        WHERE hp.ensemble_max IS NOT NULL
          AND ha.actual_max_f IS NOT NULL
          AND hp.target_date >= ?
        """,
        (today, cutoff),
    ).fetchall()
    tau = float(half_life_days) / math.log(2.0) if half_life_days > 0 else 1e9
    agg: Dict = {}
    for r in rows:
        city = r["city"]
        h = r["horizon"] or 0
        bucket = "short" if h <= 24 else ("mid" if h <= 72 else "long")
        days_old = max(0.0, float(r["days_old"] or 0))
        w = math.exp(-days_old / tau) if tau > 0 else 1.0
        residual = float(r["actual_max_f"]) - float(r["ensemble_max"])
        key = (city, bucket)
        cur = agg.get(key) or {"sum_w": 0.0, "sum_wr": 0.0, "n_raw": 0}
        cur["sum_w"]  += w
        cur["sum_wr"] += w * residual
        cur["n_raw"] += 1
        agg[key] = cur
    out: Dict = {}
    for key, v in agg.items():
        if v["sum_w"] <= 0:
            continue
        out[key] = {
            "mean_residual": round(v["sum_wr"] / v["sum_w"], 3),
            "n_eff": round(v["sum_w"], 2),
            "n_raw": v["n_raw"],
        }
    return out


def upsert_ml_replay_row(
    city: str,
    target_date: str,
    forecast_horizon_hours: int,
    ml_max: float,
) -> None:
    """Insert a single replayed (synthetic) ML prediction. Idempotent
    on (city, target_date, forecast_horizon_hours) — re-running the
    replay overwrites existing rows."""
    c = _conn_or_init()
    with _lock:
        c.execute(
            """INSERT INTO ml_historical_predictions
               (city, target_date, forecast_horizon_hours, ml_max, replayed_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(city, target_date, forecast_horizon_hours) DO UPDATE SET
                   ml_max = excluded.ml_max,
                   replayed_at = excluded.replayed_at""",
            (city, target_date, int(forecast_horizon_hours), float(ml_max), int(_time.time())),
        )
        c.commit()


def count_ml_replay_rows() -> int:
    c = _conn_or_init()
    return int(c.execute("SELECT COUNT(*) FROM ml_historical_predictions").fetchone()[0])


def compute_ml_city_bias(half_life_days: float = 30.0, lookback_days: int = 180) -> Dict:
    """Per-(city, horizon-bucket) bias of the ML MODEL's predictions vs the
    NWS-published actual. Mirrors compute_city_bias but pulls predictions
    from feature_snapshots.ml_max (live ML output captured at snapshot
    time) instead of historical_predictions.ensemble_max. Lets us correct
    ml_max separately from the ensemble — they have different residual
    profiles.

    Horizon = (target_date midnight − snapshot ts) in hours, computed
    server-side via julianday arithmetic. snapshots taken inside the
    same day fall in 'short'; previous-day-evening snapshots fall in
    'mid'; etc. Same buckets as compute_city_bias for consistency.

    Returns the same shape as compute_city_bias.
    """
    import math
    c = _conn_or_init()
    today = datetime.utcnow().date().isoformat()
    cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).date().isoformat()
    # julianday(target_date) treats the date as midnight. ts is unix
    # epoch seconds; convert to julianday by dividing by 86400 and
    # adding the unix epoch's julianday (2440587.5). Lead time in hours
    # = (julianday(target_date) − julianday_from_ts) * 24.
    rows = c.execute(
        """
        WITH residuals AS (
            -- Real snapshots (live capture)
            SELECT fs.city,
                   fs.ml_max,
                   ha.actual_max_f,
                   (julianday(fs.target_date) - (fs.ts / 86400.0 + 2440587.5)) * 24.0 AS lead_hours,
                   julianday(?) - julianday(fs.target_date) AS days_old
            FROM feature_snapshots fs
            JOIN historical_actuals ha
              ON ha.city = fs.city AND ha.target_date = fs.target_date
            WHERE fs.ml_max IS NOT NULL
              AND ha.actual_max_f IS NOT NULL
              AND fs.target_date >= ?
            UNION ALL
            -- Replayed ML predictions (synthetic; backfilled via the
            -- ml_replay tool against historical_predictions). Use the
            -- stored horizon directly since there's no ts to derive from.
            SELECT mhp.city,
                   mhp.ml_max,
                   ha.actual_max_f,
                   CAST(mhp.forecast_horizon_hours AS REAL) AS lead_hours,
                   julianday(?) - julianday(mhp.target_date) AS days_old
            FROM ml_historical_predictions mhp
            JOIN historical_actuals ha
              ON ha.city = mhp.city AND ha.target_date = mhp.target_date
            WHERE ha.actual_max_f IS NOT NULL
              AND mhp.target_date >= ?
        )
        SELECT * FROM residuals
        """,
        (today, cutoff, today, cutoff),
    ).fetchall()
    tau = float(half_life_days) / math.log(2.0) if half_life_days > 0 else 1e9
    agg: Dict = {}
    for r in rows:
        city = r["city"]
        lead = float(r["lead_hours"] or 0)
        bucket = "short" if lead <= 24 else ("mid" if lead <= 72 else "long")
        days_old = max(0.0, float(r["days_old"] or 0))
        w = math.exp(-days_old / tau) if tau > 0 else 1.0
        residual = float(r["actual_max_f"]) - float(r["ml_max"])
        key = (city, bucket)
        cur = agg.get(key) or {"sum_w": 0.0, "sum_wr": 0.0, "n_raw": 0}
        cur["sum_w"]  += w
        cur["sum_wr"] += w * residual
        cur["n_raw"] += 1
        agg[key] = cur
    out: Dict = {}
    for key, v in agg.items():
        if v["sum_w"] <= 0:
            continue
        out[key] = {
            "mean_residual": round(v["sum_wr"] / v["sum_w"], 3),
            "n_eff": round(v["sum_w"], 2),
            "n_raw": v["n_raw"],
        }
    return out
