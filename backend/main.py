"""Bets — Kalshi Weather Backend.

Phase 1 ingest (per kalshi_weather_docs_v3.docx §5):
  - Open-Meteo surface + 850/700/500mb pressure levels (HRRR/GFS seamless)
  - Open-Meteo ECMWF IFS daily max
  - IEM Mesonet GFS-MOS + NAM-MOS station bulletins
  - NWS Area Forecast Discussion (AFD) text + parser
  - NWS official daily forecast

Run:
    pip install -r requirements.txt
    uvicorn backend.main:app --reload --port 8000

The frontend (Bets.html at repo root) hits http://localhost:8000/api/state.
If the backend is unreachable, the frontend keeps running on its built-in
mock data.
"""

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


def _load_dotenv(path: Path) -> None:
    """Tiny stdlib KEY=value loader — no python-dotenv dep needed.
    Ignores blank lines and lines starting with `#`. Strips surrounding
    quotes if present. Always overwrites — every uvicorn reload picks
    up the current .env without needing a full process restart."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


_load_dotenv(Path(__file__).parent / ".env")

from . import db, kalshi
from .cities import CITIES
from .ml import predict as ml_predict
from .ml import train as ml_train
from .ml import backfill as ml_backfill
from .ml import diagnostics as ml_diagnostics
from .ml import explain as ml_explain
from .ml import online as ml_online
from .ml import backtest as ml_backtest
from .model import parse_climate_max_yesterday, settle_pl
from .sources import fetch_climate_report

CITY_TZ = {
    "ET": "America/New_York",
    "CT": "America/Chicago",
    "MT": "America/Denver",
    "PT": "America/Los_Angeles",
}


def _city_today(city_tz: str, when: Optional[datetime] = None) -> str:
    """City's current local date as YYYY-MM-DD."""
    when = when or datetime.now(timezone.utc)
    tzname = CITY_TZ.get(city_tz)
    if ZoneInfo is None or tzname is None:
        return when.date().isoformat()
    return when.astimezone(ZoneInfo(tzname)).date().isoformat()


def _city_target_date(city: Dict, when: Optional[datetime] = None) -> str:
    """The date a bet placed now should settle against = city's local
    tomorrow. Daily-max brackets are for the next day's settlement."""
    when = when or datetime.now(timezone.utc)
    tzname = CITY_TZ.get(city["tz"])
    if ZoneInfo is None or tzname is None:
        return (when + timedelta(days=1)).date().isoformat()
    local = when.astimezone(ZoneInfo(tzname))
    return (local + timedelta(days=1)).date().isoformat()

SNAPSHOT_INTERVAL_SECONDS = int(os.getenv("BETS_SNAPSHOT_INTERVAL", "300"))  # 5 min default
SNAPSHOT_LOOP_DISABLED = os.getenv("BETS_DISABLE_SNAPSHOT_LOOP") == "1"
SETTLEMENT_INTERVAL_SECONDS = int(os.getenv("BETS_SETTLEMENT_INTERVAL", "3600"))  # 1 hour default
SETTLEMENT_LOOP_DISABLED = os.getenv("BETS_DISABLE_SETTLEMENT_LOOP") == "1"

# Auto paper-trader. Initial config from env, runtime-mutable via
# POST /api/auto-trade/config. The loop reads from this dict on every
# iteration so toggles take effect on the next tick (no restart needed).
_auto_trade_state: Dict = {
    "enabled": os.getenv("BETS_AUTO_TRADE") == "1",
    "min_edge_cents": int(os.getenv("BETS_AUTO_TRADE_MIN_EDGE", "5")),
    # bankroll has been retired. All sizing reads the live account balance
    # from the ledger (db.account_balance) so the dollar amount tracks
    # exactly what's deployable. Use deposits / withdrawals to grow or
    # shrink the trading account.
    # Hard caps on a single bet. Two modes — set either or both:
    #   max_pct_of_balance: fluid cap as a fraction of account balance.
    #     The auto-trader recomputes the dollar value each tick, so it
    #     tracks the live account balance. Default 12% (just above the
    #     highest tier so tiers can play out).
    #   max_usd: absolute dollar ceiling. Used as a safety belt on top of
    #     the percent cap — whichever is SMALLER binds. Set to a very
    #     large number to effectively disable.
    "max_pct_of_balance": float(os.getenv("BETS_AUTO_TRADE_MAX_PCT", "0.12")),
    "max_usd": float(os.getenv("BETS_AUTO_TRADE_MAX_USD", "10000")),
    "min_usd": float(os.getenv("BETS_AUTO_TRADE_MIN_USD", "50")),
    # Hard cap on bets per (city, target_date) to stop neighbor-bracket
    # spam (e.g., NYC ≤77, NYC ≥77, NYC 77-78, NYC 76-77 all on the same
    # day — they overlap and we end up betting against ourselves).
    "max_bets_per_city_per_day": int(os.getenv("BETS_AUTO_TRADE_MAX_PER_CITY_DAY", "2")),
    # Quality filters — prevent garbage long-shot bets that have "edge"
    # on paper but lose 95% of the time:
    #   min_model_pct: refuse to buy YES below this model probability.
    #     A 3¢ edge over a 1¢ market = model says 4% — still loses 96%
    #     of the time. Default 12% knocks out the worst tails.
    #   min_kelly: refuse if Kelly says we should risk less than this
    #     fraction of bankroll (i.e., the bet isn't materially +EV).
    #   skip_extreme_entry_cents: hard skip on entries under N cents.
    #     These are pure variance plays — even a "right" prediction
    #     leaves you wrong most days.
    "min_model_pct": float(os.getenv("BETS_AUTO_TRADE_MIN_MODEL_PCT", "0.12")),
    "min_kelly": float(os.getenv("BETS_AUTO_TRADE_MIN_KELLY", "0.005")),
    "skip_entry_below_cents": int(os.getenv("BETS_AUTO_TRADE_SKIP_BELOW", "5")),
    # Bracket-quality gates discovered from settled history:
    #   require_peak_in_bracket: refuse a non-tail bracket whose [lo, hi]
    #     doesn't contain the model's predicted peak. The auto-trader's
    #     "highest edge" sort otherwise picks 1°-wide brackets that the
    #     market underprices precisely because they're unlikely — fake
    #     edge that cost +$3.8k in the first 17 bets. Tail brackets
    #     (≤X / ≥X) are exempt since their range is open-ended.
    #   prevent_overlapping_brackets: skip a candidate whose [lo, hi]
    #     overlaps any already-open bet for the same (city, target_date).
    #     Same-day adjacent brackets are mutually exclusive — only one
    #     can settle YES — so layering them just doubles risk for no
    #     extra upside.
    "require_peak_in_bracket": os.getenv("BETS_AUTO_TRADE_REQUIRE_PEAK", "1") != "0",
    "prevent_overlapping_brackets": os.getenv("BETS_AUTO_TRADE_PREVENT_OVERLAP", "1") != "0",
    # Stop-loss floor — the auto-trader refuses to place a bet that would
    # bring `available_cash - size` below this number. Acts as a circuit
    # breaker so a runaway sweep can't drain the account to zero.
    "min_balance_usd": float(os.getenv("BETS_AUTO_TRADE_MIN_BALANCE", "100")),
    # Virtual paper-trading mode. When on, ALL sizing decisions read
    # from `virtual_current_balance` instead of (bankroll + realized_pl).
    # Each placement deducts the stake; each winning settlement credits
    # the gross payout (stake + profit). Lets you A/B test the gates,
    # tier table, etc. against a stable seed without touching the live
    # Kalshi account math. Persists to backend/auto_trade_state.json so
    # it survives restarts.
    "virtual_mode": os.getenv("BETS_VIRTUAL_MODE", "0") == "1",
    "virtual_starting_balance": float(os.getenv("BETS_VIRTUAL_START", "1000")),
    "virtual_current_balance": float(os.getenv("BETS_VIRTUAL_START", "1000")),
    # Bank-account mode. Strictly supersedes virtual_mode and bankroll
    # for sizing when on. The auto-trader runs against db.account_balance()
    # exclusively: deposits / withdrawals / bet stakes / payouts all flow
    # through the account_transactions ledger so the dollar number can
    # always be reconciled against SUM(amount). Live Kalshi cash is
    # ignored entirely. Default ON (per spec) — but balance starts at $0
    # so the trader gracefully no-ops until the user makes a deposit.
    "account_mode": os.getenv("BETS_ACCOUNT_MODE", "1") == "1",
    "account_balance": float(os.getenv("BETS_ACCOUNT_START", "0")),
    # Bet-sizing strategy:
    #   "kelly" — size = round(kelly_fraction * available_cash), clamped to
    #             [min_usd, max_usd]. Theoretically optimal but the dollar
    #             amount jumps around with edge ¢ AND price.
    #   "tiers" (default) — bucket the edge into confidence tiers, each
    #             specifying a PERCENT of account equity to stake. Stakes
    #             scale fluidly as the account grows or shrinks. Highest
    #             min-edge match wins; below-threshold falls through to
    #             kelly. Final stake is always clamped to available cash
    #             and [min_usd, max_usd].
    "bet_sizing_mode": os.getenv("BETS_AUTO_TRADE_SIZING", "tiers"),
    "size_tiers": [
        {"min_edge_cents": 12, "pct_of_balance": 0.10},   # 10% of equity
        {"min_edge_cents": 8,  "pct_of_balance": 0.075},  # 7.5%
        {"min_edge_cents": 5,  "pct_of_balance": 0.05},   # 5%
        {"min_edge_cents": 3,  "pct_of_balance": 0.025},  # 2.5%
    ],
    # Interval is now part of mutable state — loop reads it each
    # iteration so /api/auto-trade/config can change cadence at runtime.
    "interval_seconds": int(os.getenv("BETS_AUTO_TRADE_INTERVAL", "600")),
    "last_run_ts": None,
    "last_run_placed": 0,
    "last_run_considered": 0,
    "total_runs": 0,
    "total_placed": 0,
}

_snapshot_task: Optional[asyncio.Task] = None
_settlement_task: Optional[asyncio.Task] = None
_auto_trader_task: Optional[asyncio.Task] = None
_ml_retrain_task: Optional[asyncio.Task] = None
_ml_incremental_backfill_task: Optional[asyncio.Task] = None

# ML retraining loop. Default daily so the model keeps absorbing newly-
# settled days as the historical_actuals table grows.
ML_RETRAIN_INTERVAL_SECONDS = int(os.getenv("BETS_ML_RETRAIN_INTERVAL", "86400"))
# Wait this many seconds after process startup before the first retrain
# fires. The training itself runs in a thread (asyncio.to_thread) so it
# doesn't block HTTP, but we still defer the first sweep so the server
# is responsive immediately after boot.
ML_RETRAIN_INITIAL_DELAY_SECONDS = int(os.getenv("BETS_ML_RETRAIN_INITIAL_DELAY", "60"))
ML_RETRAIN_LOOP_DISABLED = os.getenv("BETS_DISABLE_ML_RETRAIN_LOOP") == "1"

# Incremental-backfill loop: every ML_BACKFILL_INTERVAL_SECONDS, pull
# the last 7 days into historical_predictions / historical_actuals so
# the next retrain has fresh examples. Idempotent thanks to
# INSERT OR REPLACE in the upsert helpers.
ML_BACKFILL_INTERVAL_SECONDS = int(os.getenv("BETS_ML_BACKFILL_INTERVAL", "86400"))
ML_BACKFILL_LOOP_DISABLED = os.getenv("BETS_DISABLE_ML_BACKFILL_LOOP") == "1"

# Read-only SQL console. Disabled by default outside localhost dev — set
# BETS_DISABLE_SQL_CONSOLE=1 to turn it off entirely. Enforces SELECT-only,
# single-statement, hard row cap.
SQL_CONSOLE_DISABLED = os.getenv("BETS_DISABLE_SQL_CONSOLE") == "1"
SQL_CONSOLE_ROW_CAP = int(os.getenv("BETS_SQL_CONSOLE_ROW_CAP", "1000"))

# Blend weight for blended_max = α · ml_max + (1−α) · model_max.
# Runtime-mutable model controls (UI can change without restart).
# Defaults seeded from env vars. _model_state is read every state build.
_model_state: Dict = {
    "modelmax_source": os.getenv("BETS_MODELMAX_SOURCE", "ml").lower(),
    "blend_alpha": float(os.getenv("BETS_BLEND_ALPHA", "0.7")),
    # Per-city bias correction (decaying-weight residuals from
    # historical_predictions × historical_actuals). Disable by setting
    # bias_correction=false. Tunable via /api/bias/config.
    "bias_correction": os.getenv("BETS_BIAS_CORRECTION", "1") != "0",
    "bias_half_life_days": float(os.getenv("BETS_BIAS_HALF_LIFE_DAYS", "30")),
    "bias_lookback_days": int(os.getenv("BETS_BIAS_LOOKBACK_DAYS", "180")),
    "bias_max_correction": float(os.getenv("BETS_BIAS_MAX_CORRECTION", "5.0")),
}

# Backfill progress shared dict (read by /api/ml/backfill/status).
_ml_backfill_progress: Dict = {"running": False, "stage": "idle"}

# In-memory cache for the per-city bias map. Rebuilt from the DB on a TTL
# (default 1h) so HTTP requests aren't paying the cost of a 180-day join
# every state build. Invalidate via /api/bias/refresh after a settlement.
_bias_cache: Dict = {"map": None, "computed_at": 0.0}
# Same shape, but for the ML model output specifically (joined from
# feature_snapshots × historical_actuals so we capture residuals at each
# snapshot's actual lead time, not just the daily ensemble).
_ml_bias_cache: Dict = {"map": None, "computed_at": 0.0}
_BIAS_CACHE_TTL_SECONDS = int(os.getenv("BETS_BIAS_CACHE_TTL", "3600"))


def _get_ml_bias_map() -> Dict:
    """ML-specific bias map: per-(city, horizon-bucket) residual of
    feature_snapshots.ml_max vs historical_actuals.actual_max_f. Cached
    with the same TTL as the ensemble map. Falls back to empty on error."""
    now = time.time()
    cached_map = _ml_bias_cache.get("map")
    cached_at = _ml_bias_cache.get("computed_at") or 0
    if cached_map is not None and (now - cached_at) < _BIAS_CACHE_TTL_SECONDS:
        return cached_map
    try:
        bias = db.compute_ml_city_bias(
            half_life_days=float(_model_state.get("bias_half_life_days") or 30),
            lookback_days=int(_model_state.get("bias_lookback_days") or 180),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[bias] ml compute failed: {exc}")
        bias = cached_map or {}
    _ml_bias_cache["map"] = bias
    _ml_bias_cache["computed_at"] = now
    return bias


def _ml_bias_correction_for(city_code: str, target_date: Optional[str]) -> float:
    """Lookup the additive correction (°F) for the ML model output for
    this city + horizon. Same toggles + cap as the ensemble bias path."""
    cfg = _model_state
    if not cfg.get("bias_correction", True):
        return 0.0
    if not city_code:
        return 0.0
    bucket = "short"
    if target_date:
        try:
            tgt = datetime.fromisoformat(target_date).date()
            days_out = (tgt - datetime.utcnow().date()).days
            hours = days_out * 24
            bucket = "short" if hours <= 24 else ("mid" if hours <= 72 else "long")
        except (ValueError, TypeError):
            pass
    bmap = _get_ml_bias_map() or {}
    entry = bmap.get((city_code, bucket))
    if entry is None:
        return 0.0
    cap = float(cfg.get("bias_max_correction") or 5.0)
    return max(-cap, min(cap, float(entry.get("mean_residual") or 0.0)))


def _get_bias_map() -> Dict:
    """Return the (city, horizon-bucket) → mean_residual map, recomputing
    from the DB if the cache is stale or empty. Read-mostly: every state
    build calls this; the underlying SQL only runs once per TTL window."""
    now = time.time()
    cached_map = _bias_cache.get("map")
    cached_at = _bias_cache.get("computed_at") or 0
    if cached_map is not None and (now - cached_at) < _BIAS_CACHE_TTL_SECONDS:
        return cached_map
    try:
        bias = db.compute_city_bias(
            half_life_days=float(_model_state.get("bias_half_life_days") or 30),
            lookback_days=int(_model_state.get("bias_lookback_days") or 180),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[bias] compute failed: {exc}")
        bias = cached_map or {}
    _bias_cache["map"] = bias
    _bias_cache["computed_at"] = now
    return bias


def _bias_correction_for(city_code: str, target_date: Optional[str]) -> float:
    """Look up the additive correction (in °F) to apply to the raw model_max
    for this city + forecast horizon. Returns 0.0 if disabled, no data, or
    correction is clamped to zero."""
    cfg = _model_state
    if not cfg.get("bias_correction", True):
        return 0.0
    if not city_code:
        return 0.0
    # Horizon bucket: derive from days-until-target. If we can't parse the
    # date, default to "short" (most conservative — least far-out forecast).
    bucket = "short"
    if target_date:
        try:
            tgt = datetime.fromisoformat(target_date).date()
            days_out = (tgt - datetime.utcnow().date()).days
            hours = days_out * 24
            bucket = "short" if hours <= 24 else ("mid" if hours <= 72 else "long")
        except (ValueError, TypeError):
            pass
    bmap = _get_bias_map() or {}
    entry = bmap.get((city_code, bucket))
    if entry is None:
        return 0.0
    cap = float(cfg.get("bias_max_correction") or 5.0)
    return max(-cap, min(cap, float(entry.get("mean_residual") or 0.0)))
from .model import (
    best_bracket,
    bracket_prob,
    compute_ladder,
    ensemble_model_max,
    kelly_fraction,
    live_style_ensemble,
    parse_afd,
)
from .sources import (
    fetch_afd,
    fetch_ecmwf_max,
    fetch_iem_mos,
    fetch_metar,
    fetch_nws_forecast_max,
    fetch_nws_forecast_periods,
    fetch_open_meteo,
    fetch_sounding,
    fetch_sounding_raw,
)

CACHE_TTL_SECONDS = 600  # 10 min — Open-Meteo refreshes hourly, MOS/AFD every 6h
_state_cache: Dict[str, Dict] = {}


def c_to_f(c: Optional[float]) -> Optional[float]:
    return None if c is None else c * 9 / 5 + 32


def _afternoon_index(times: List[str]) -> int:
    """Pick the hourly index closest to 18:00 local on day index 1 (tomorrow)."""
    target = None
    for i, t in enumerate(times):
        if "T18:00" in t and i >= 24:
            return i
    return min(36, len(times) - 1)


async def _build_city_state(client: httpx.AsyncClient, city: Dict) -> Optional[Dict]:
    target_iso = _city_target_date(city)
    om, ecmwf_c, gfs_mos_f, nam_mos_f, afd_text, nws_max_f, sounding, metar = await asyncio.gather(
        fetch_open_meteo(client, city["lat"], city["lon"]),
        fetch_ecmwf_max(client, city["lat"], city["lon"]),
        fetch_iem_mos(client, city["station"], "GFS"),
        fetch_iem_mos(client, city["station"], "NAM"),
        fetch_afd(client, city["office"]),
        fetch_nws_forecast_max(client, city["lat"], city["lon"], target_date_iso=target_iso),
        fetch_sounding(client, city["sounding"]),
        fetch_metar(client, city["station"]),
    )

    if not om:
        return None

    daily = om.get("daily", {})
    hourly = om.get("hourly", {})
    times = hourly.get("time", [])
    if len(daily.get("temperature_2m_max") or []) < 2 or not times:
        return None

    om_max_f = c_to_f(daily["temperature_2m_max"][1])
    # Prefer the METAR reading at the exact ASOS station — it's what Kalshi
    # settles against. Fall back to Open-Meteo's blended hourly when METAR
    # is unavailable (e.g., outage or upstream timeout).
    obs_now_f = c_to_f(metar["temp_c"]) if metar else c_to_f(hourly["temperature_2m"][0])
    idx = _afternoon_index(times)
    t850_c = hourly["temperature_850hPa"][idx]
    t700_c = hourly["temperature_700hPa"][idx]
    t500_c = hourly["temperature_500hPa"][idx]
    h500 = hourly["geopotential_height_500hPa"][idx]
    rh850 = hourly["relative_humidity_850hPa"][idx]
    wdir = hourly["wind_direction_850hPa"][idx]
    wspd = hourly["wind_speed_850hPa"][idx]
    ecmwf_max_f = c_to_f(ecmwf_c)

    # Today's max-so-far (observed) + remaining-day forecast max — used by
    # the open-positions table to flag bets that have already locked in a
    # win/loss before the official close.
    tzname = CITY_TZ.get(city.get("tz"))
    if ZoneInfo is not None and tzname:
        local_now = datetime.now(ZoneInfo(tzname))
    else:
        local_now = datetime.now(timezone.utc)
    today_str = local_now.date().isoformat()
    now_iso_hour = local_now.strftime("%Y-%m-%dT%H:00")
    past_temps = []
    future_temps = []
    for t_iso, t_c in zip(times, hourly["temperature_2m"]):
        if not t_iso.startswith(today_str) or t_c is None:
            continue
        f = c_to_f(t_c)
        if t_iso <= now_iso_hour:
            past_temps.append(f)
        else:
            future_temps.append(f)
    max_so_far_f = max(past_temps) if past_temps else None
    if metar and max_so_far_f is not None:
        max_so_far_f = max(max_so_far_f, c_to_f(metar["temp_c"]))
    forecast_remainder_max_f = max(future_temps) if future_temps else None
    # Capture the calendar date the live observations actually cover so
    # _bet_live_status can refuse to score a bet whose target_date doesn't
    # match. (Distinct from `targetDate` below, which is the date a NEW
    # bet placed right now would settle against — that flips to tomorrow
    # after the daily cutoff.)
    data_date_local = today_str

    # Doc §2.1: MOS is the highest-value source; weight it accordingly.
    # Weights live in model.LIVE_ENSEMBLE_WEIGHTS so the historical recompute
    # pass (db.recompute_historical_ensemble_max) produces the SAME ensemble
    # formula that lives here — no more train/inference covariate shift on
    # the ensemble_max feature.
    raw_model_max = live_style_ensemble(
        gfs_mos_f, nam_mos_f, nws_max_f, om_max_f, ecmwf_max_f,
    )
    # Apply per-city, per-horizon bias correction if enabled. The raw
    # ensemble systematically under-forecasts most cities by 0.5-1°F (see
    # /api/bias for the running residuals); shifting the headline by that
    # amount turns narrow brackets from systematic losers into honest 50/50s.
    bias_shift = _bias_correction_for(city.get("code"), target_iso) if raw_model_max is not None else 0.0
    model_max = raw_model_max + bias_shift if raw_model_max is not None else None
    if model_max is None:
        return None

    # Run the trained ML model in parallel for the comparison panel.
    # Pass the same feature set the training schema uses, including
    # yesterday's actual + monthly climatology if available.
    prev_actual = db.get_prev_actual(city["code"], target_iso) if target_iso else None
    seasonal_avg = db.get_seasonal_avg(city["code"], target_iso) if target_iso else None
    lag3 = db.get_lag_actual(city["code"], target_iso, 3) if target_iso else None
    lag7 = db.get_lag_actual(city["code"], target_iso, 7) if target_iso else None
    roll7 = db.get_rolling_mean_actual(city["code"], target_iso, days=7) if target_iso else None
    ml_features = {
        "gfs_max": om_max_f,        # surface OM seamless ≈ GFS daily max
        "ecmwf_max": ecmwf_max_f,
        "icon_max": None,           # not currently in live state
        "om_max": om_max_f,
        "gfs_mos_max": gfs_mos_f,
        "nam_mos_max": nam_mos_f,
        "t850_c": t850_c,
        "t700_c": t700_c,
        "t500_c": t500_c,
        "h500_m": h500,
        "rh850_pct": rh850,
        "ensemble_max": model_max,
        "prev_actual_max_f": prev_actual,
        "lag3_actual_max_f": lag3,
        "lag7_actual_max_f": lag7,
        "roll7_mean_max_f": roll7,
        "seasonal_avg_max_f": seasonal_avg,
    }
    raw_ml_max = ml_predict.predict_max(ml_features, city=city["code"])
    # Per-city ML bias correction. Computed from feature_snapshots.ml_max
    # × historical_actuals so the residual reflects the trained model's
    # actual track record per city, not the ensemble's. Symmetric with
    # the ensemble bias path; same toggle + cap.
    ml_bias_shift = _ml_bias_correction_for(city.get("code"), target_iso) if raw_ml_max is not None else 0.0
    ml_max = raw_ml_max + ml_bias_shift if raw_ml_max is not None else None
    # Quantile band (p10/p50/p90). Apply the same bias shift so the
    # interpolated CDF stays centered on the corrected point estimate.
    ml_quantiles_raw = ml_predict.predict_quantiles(ml_features, city=city["code"])
    ml_quantiles = None
    if ml_quantiles_raw:
        ml_quantiles = {
            k: (v + ml_bias_shift) if v is not None else None
            for k, v in ml_quantiles_raw.items()
        }
    # Conformal prediction interval — distribution-free 90% coverage.
    ml_conformal = ml_predict.conformal_interval(ml_features, city=city["code"])

    afd = parse_afd(afd_text)
    # Next-day temp forecasts have ~1.5-2°F std dev empirically, so default
    # σ = 1.8 with AFD adjustment. Tighter when AFD says high confidence,
    # wider when models disagree. Old σ=2.4 was calibrated for the
    # synthetic 2°F-wide ladder; the live Kalshi 1°F brackets need tighter.
    sigma = 1.8 - (afd["score"] - 3) * 0.20
    sigma = max(1.0, sigma)

    # Choose which prediction drives the bracket-prob math (and therefore
    # the recommended bet + edge calc + auto-trader). Modes:
    #   ensemble - naive weighted average (legacy behavior)
    #   ml       - trained ML model only
    #   blend    - α·ml + (1-α)·ensemble (default; safest mix)
    #   auto     - blend if ml available, else ensemble
    # ML wins on holdout but the live ensemble has access to MOS that the
    # trained model didn't see in backfill, so blend captures both.
    blend_alpha = float(_model_state.get("blend_alpha", 0.7))
    blended_for_ladder = (
        blend_alpha * ml_max + (1 - blend_alpha) * model_max
        if ml_max is not None else None
    )
    src = str(_model_state.get("modelmax_source", "auto")).lower()
    if src == "ml" and ml_max is not None:
        active_max, active_source = ml_max, "ml"
    elif src == "blend" and blended_for_ladder is not None:
        active_max, active_source = blended_for_ladder, "blend"
    elif src == "ensemble":
        active_max, active_source = model_max, "ensemble"
    else:  # auto
        if blended_for_ladder is not None:
            active_max, active_source = blended_for_ladder, "blend"
        else:
            active_max, active_source = model_max, "ensemble"

    kalshi_brackets = None
    if kalshi.configured() and city.get("kalshi_series"):
        kalshi_brackets = await kalshi.fetch_brackets_for_city(client, city["kalshi_series"])

    # Quantile trio for bracket-prob calibration. Only meaningful when
    # the ML model is supplying the active prediction (ensemble path
    # has no per-day uncertainty estimate). Falls through to fixed-σ
    # Gaussian when missing or when active_source != 'ml'/'blend'.
    quantile_trio = None
    if active_source in ("ml", "blend") and ml_quantiles:
        p10 = ml_quantiles.get("p10")
        p50 = ml_quantiles.get("p50")
        p90 = ml_quantiles.get("p90")
        if p10 is not None and p50 is not None and p90 is not None:
            quantile_trio = {"p10": p10, "p50": p50, "p90": p90}

    if kalshi_brackets:
        ladder = []
        for b in kalshi_brackets:
            model_pct = bracket_prob(
                b["lo"], b["hi"], active_max, sigma,
                lower_tail=b.get("lower_tail", False),
                upper_tail=b.get("upper_tail", False),
                quantiles=quantile_trio,
            )
            kalshi_pct = b["yes_cents"] / 100
            ladder.append({
                "lo": b["lo"], "hi": b["hi"],
                "label": f"{b['lo']}–{b['hi']}°F" if not (b.get("lower_tail") or b.get("upper_tail"))
                         else (f"≤{b['hi']}°F" if b.get("lower_tail") else f"≥{b['lo']}°F"),
                "modelPct": round(model_pct, 4),
                "kalshiPct": round(kalshi_pct, 4),
                "edge": round(model_pct - kalshi_pct, 4),
                "yesPrice": b["yes_cents"],
                "volume": b["volume"],
                "kalshiTicker": b["ticker"],
            })
    else:
        ladder = compute_ladder(active_max, model_sigma=sigma, kalshi_widening=1.30)

    best = best_bracket(ladder)
    kelly = kelly_fraction(best["edge"], best["kalshiPct"])

    lapse = round((c_to_f(t850_c) - obs_now_f) / -1.5, 1) if obs_now_f is not None else 6.5

    # Doc §2.4 — observed 12Z sounding gives ground-truth atmospheric state.
    # The 850mb obs-vs-model delta is one of the highest-value edge signals.
    s850 = (sounding or {}).get(850) if sounding else None
    s700 = (sounding or {}).get(700) if sounding else None
    if s850:
        snd_t850_obs = s850["temp_c"]
        snd_t850_delta = round(s850["temp_c"] - t850_c, 2)
        snd_depression = round(s850["temp_c"] - s850["dwpt_c"], 1)
    else:
        snd_t850_obs = t850_c
        snd_t850_delta = 0.0
        snd_depression = 3.0
    snd_lapse = abs(lapse)
    if s850 and s700:
        # Observed lapse rate between 700 and 850mb (~1.5 km layer), °C/km.
        snd_lapse = round(abs(s700["temp_c"] - s850["temp_c"]) / 1.5, 1)

    return {
        "city": {k: city[k] for k in ("code", "station", "label", "office", "tz")},
        "asof": int(time.time() * 1000),
        "targetDate": target_iso,
        "sigmaUsed": round(sigma, 3),
        "ecmwfMax": round(ecmwf_max_f, 1) if ecmwf_max_f is not None else None,
        "omMax": round(om_max_f, 1) if om_max_f is not None else None,
        "afdAboveNormal": afd.get("above_normal", False),
        "settlementBracket": best,
        "modelMax": round(model_max, 1),
        "rawModelMax": round(raw_model_max, 1) if raw_model_max is not None else None,
        "rawMlMax": round(raw_ml_max, 1) if raw_ml_max is not None else None,
        "mlBiasShift": round(ml_bias_shift, 2),
        "biasShift": round(bias_shift, 2),
        "mlMax": ml_max,
        "mlP10": (ml_quantiles or {}).get("p10"),
        "mlP50": (ml_quantiles or {}).get("p50"),
        "mlP90": (ml_quantiles or {}).get("p90"),
        "mlConformal": ml_conformal,
        "blendedMax": (
            round(blend_alpha * ml_max + (1 - blend_alpha) * model_max, 1)
            if ml_max is not None else None
        ),
        "activeMax": round(active_max, 1),
        "activeSource": active_source,
        "nwsForecast": round(nws_max_f, 1) if nws_max_f is not None else round(om_max_f, 1),
        "mosMax": round(gfs_mos_f, 1) if gfs_mos_f is not None else round(model_max, 1),
        "namMos": round(nam_mos_f, 1) if nam_mos_f is not None else round(model_max, 1),
        "obsCurrent": round(obs_now_f, 1) if obs_now_f is not None else round(model_max - 6, 1),
        "maxSoFarF": round(max_so_far_f, 1) if max_so_far_f is not None else None,
        "forecastRemainderMaxF": round(forecast_remainder_max_f, 1) if forecast_remainder_max_f is not None else None,
        "dataDate": data_date_local,
        "confidence": round(0.4 + afd["score"] * 0.1, 2),
        "afdConfScore": afd["score"],
        "brackets": ladder,
        "bestEdgeCents": int(round(best["edge"] * 100)),
        "kellyPct": round(kelly, 3),
        "upperAir": {
            "t850": round(t850_c, 1),
            "t700": round(t700_c, 1),
            "t500": round(t500_c, 1),
            "h500": int(round(h500)),
            "lapse": round(abs(lapse), 1),
            "windDir": int(round(wdir)),
            "windKt": int(round(wspd)),
            "rh850": int(round(rh850)),
        },
        "sounding": {
            "t850Obs": round(snd_t850_obs, 1),
            "t850Delta": snd_t850_delta,
            "lapse": snd_lapse,
            "depression": snd_depression,
            "available": s850 is not None,
        },
        "afdText": afd_text or f"{city['office']} AFD unavailable. Using ensemble model output only.",
        "flags": afd["flags"],
        "sources": {
            "gfsMos": gfs_mos_f is not None,
            "namMos": nam_mos_f is not None,
            "ecmwf": ecmwf_max_f is not None,
            "nws": nws_max_f is not None,
            "afd": afd_text is not None,
            "sounding": s850 is not None,
            "metar": metar is not None,
            "kalshi": kalshi_brackets is not None,
        },
    }


async def _build_state_cached(city: Dict, client: httpx.AsyncClient) -> Optional[Dict]:
    key = city["code"]
    cached = _state_cache.get(key)
    if cached and time.time() - cached["t"] < CACHE_TTL_SECONDS:
        return cached["v"]
    state = await _build_city_state(client, city)
    if state is not None:
        _state_cache[key] = {"t": time.time(), "v": state}
    return state


app = FastAPI(title="Bets — Kalshi Weather Backend", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


async def _snapshot_tick(client: httpx.AsyncClient, ts: Optional[int] = None) -> int:
    """One iteration of the snapshot loop. Returns the count of marks
    written. Factored out so tests can drive a single tick without the
    sleep loop."""
    if ts is None:
        ts = int(time.time())
    states = await asyncio.gather(*[_build_state_cached(c, client) for c in CITIES])
    state_by_city: Dict[str, Dict] = {}
    for s in states:
        if s is not None:
            db.insert_snapshot(s, ts=ts)
            db.insert_feature_snapshot(s, ts=ts)
            state_by_city[s["city"]["code"]] = s
    if not state_by_city:
        return 0
    marks = 0
    for b in db.list_open_bets():
        cs = state_by_city.get(b["city"])
        if not cs:
            continue
        bracket = next(
            (br for br in cs["brackets"] if br["label"] == b["bracket_label"]),
            None,
        )
        if not bracket:
            continue
        current_yes = bracket["kalshiPct"]
        entry_yes = b["entry_cents"] / 100
        sign = 1 if b["side"] == "YES" else -1
        pl = (current_yes - entry_yes) * b["size"] * sign * 100
        db.insert_position_mark(b["id"], ts, current_yes, pl)
        marks += 1
    return marks


async def _snapshot_loop() -> None:
    """Persist a snapshot of every city's state on a fixed cadence and mark
    every open bet against it. Snapshots are skipped when upstream is
    unreachable (state is None) so we never poison the time series with
    synthetic fallback data; bets without a usable snapshot for their city
    are skipped at the same tick."""
    while True:
        try:
            async with httpx.AsyncClient() as client:
                await _snapshot_tick(client)
        except Exception as exc:  # noqa: BLE001 — best-effort background task
            print(f"[bets] snapshot loop error: {exc}")
        await asyncio.sleep(SNAPSHOT_INTERVAL_SECONDS)


async def _ml_retrain_loop() -> None:
    """Retrain via the 'auto' sweep every BETS_ML_RETRAIN_INTERVAL
    seconds (default daily). Each cycle:
      - sweeps linear (Ridge) + rf + gbm hyperparameter grid
      - persists the lowest test_mae candidate as the active model
      - logs every candidate to ml_runs so the history table shows
        the full sweep and the trend sparkline traces actual progress

    Combined with _ml_incremental_backfill_loop (which extends the
    historical tables daily), the model genuinely improves over time
    as more settled data accrues AND as the sweep finds better
    configurations on that data."""
    last_paired_count = db.count_paired_total()
    while True:
        # Defer the first sweep so uvicorn lifespan startup can finish
        # before we monopolize CPU. Subsequent iterations sleep at the
        # bottom of the loop on the configured interval.
        await asyncio.sleep(ML_RETRAIN_INITIAL_DELAY_SECONDS)
        # Data-driven trigger: also fire if at least 50 new paired rows
        # have arrived since the last retrain. Logged with `trigger`
        # field in the start event so the activity feed shows why.
        cur_paired = db.count_paired_total()
        new_rows = cur_paired - last_paired_count
        trigger = "data_threshold" if new_rows >= 50 else "scheduled"
        try:
            prev = db.latest_ml_run()
            # Pre-train dedup gate. If the previous run trained on exactly
            # the same total paired set AND already has both test_mae and
            # walk_forward_mae populated, nothing has changed since — skip
            # the entire sweep instead of burning CPU on a duplicate.
            # Triggered runs (data_threshold) already imply n_paired grew,
            # so this only blocks the daily scheduled tick on a quiet day.
            #
            # NOTE: prev.n_train is the TRAIN-side of the 80/20 split, not
            # the full dataset. cur_paired counts ALL paired rows. Compare
            # against (n_train + n_test) to put them on the same scale.
            prev_paired_total = ((prev or {}).get("n_train") or 0) + ((prev or {}).get("n_test") or 0)
            if (prev
                and prev_paired_total == cur_paired
                and prev.get("test_mae") is not None
                and prev.get("walk_forward_mae") is not None
                and not prev.get("skipped")):
                db.log_ml_event(
                    "retrain_done",
                    {"skipped": True, "n_paired": cur_paired,
                     "matched_run_id": prev["id"], "reason": "no new data"},
                    f"retrain skipped — no new pairs since run {prev['id']} "
                    f"(n_paired={cur_paired:,})",
                )
                # Mark a stub row so the activity feed reflects the tick.
                fp = db.compute_ml_run_fingerprint(
                    prev["algorithm"], cur_paired, [], None,
                )
                db.log_ml_run_skipped(
                    prev["algorithm"], cur_paired, fp, prev["id"],
                )
                last_paired_count = cur_paired
                await asyncio.sleep(max(0, ML_RETRAIN_INTERVAL_SECONDS - ML_RETRAIN_INITIAL_DELAY_SECONDS))
                continue
            db.log_ml_event(
                "retrain_start",
                {"trigger": trigger, "n_paired": cur_paired, "new_since_last": new_rows},
                f"retrain started — trigger={trigger}, n_paired={cur_paired:,}",
            )
            # Training is sync and CPU-bound (sklearn). Run it in a thread
            # so the event loop stays free for HTTP traffic.
            run = await asyncio.to_thread(ml_train.train, algorithm="auto")
            if run.get("error"):
                print(f"[ml-retrain] auto skipped: {run['error']}")
                db.log_ml_event(
                    "retrain_done",
                    {"error": run["error"]},
                    f"retrain skipped: {run['error']}",
                )
            else:
                cands = run.get("auto_candidates") or []
                summary = ", ".join(f"{c['algorithm']}={c['test_mae']}" for c in cands)
                print(f"[ml-retrain] auto sweep: {summary} → kept {run.get('algorithm')}")
                db.log_ml_event(
                    "retrain_done",
                    {"algorithm": run.get("algorithm"), "test_mae": run.get("test_mae"),
                     "n_train": run.get("n_train"), "n_test": run.get("n_test"),
                     "candidates": cands},
                    f"retrain done — kept {run.get('algorithm')} "
                    f"(test_mae={run.get('test_mae')})",
                )
                # Champion selection prefers walk_forward_mae (the honest
                # time-series eval). Falls back to test_mae if WF wasn't
                # computed for either run, but a model with no WF metric
                # is ineligible to take champion from a model that has one.
                prev_wf = (prev or {}).get("walk_forward_mae")
                run_wf  = run.get("walk_forward_mae")
                metric_used = "walk_forward_mae" if (prev_wf is not None and run_wf is not None) else "test_mae"
                prev_v = prev_wf if metric_used == "walk_forward_mae" else (prev or {}).get("test_mae")
                run_v  = run_wf  if metric_used == "walk_forward_mae" else run.get("test_mae")
                if prev and prev_v and run_v:
                    delta = run_v - prev_v
                    pct = (delta / prev_v) * 100 if prev_v else 0.0
                    arrow = "↘" if delta < 0 else "↗"
                    db.log_ml_event(
                        "model_swap",
                        {"from": prev.get("algorithm"), "to": run.get("algorithm"),
                         "metric_used": metric_used,
                         f"from_{metric_used}": prev_v,
                         f"to_{metric_used}":   run_v,
                         "delta": round(delta, 4), "pct": round(pct, 2)},
                        f"model swap — {metric_used} {prev_v:.3f} {arrow} "
                        f"{run_v:.3f} ({pct:+.1f}%)",
                    )
                    # ── Champion/challenger gate ──
                    # Auto-promote new model if it beats champion by ≥5%.
                    # Otherwise mark new run as 'archived' (stays in
                    # ml_runs for diff visibility but doesn't go live).
                    # Auto-rollback (re-promote prev) if new model is
                    # >20% worse — protects against bad retrains.
                    try:
                        improved_pct = -pct  # negative pct = better
                        if improved_pct >= 5.0:
                            db.update_ml_run_role(prev["id"], "archived")
                            # New run already inserted as 'champion' default
                            db.log_ml_event(
                                "model_promoted",
                                {"new_id": run["id"], "old_id": prev["id"],
                                 "improvement_pct": round(improved_pct, 2)},
                                f"challenger promoted to champion "
                                f"(+{improved_pct:.1f}% MAE improvement)",
                            )
                        elif improved_pct < -20.0:
                            # New model is materially worse — rollback
                            db.update_ml_run_role(run["id"], "archived")
                            db.update_ml_run_role(prev["id"], "champion")
                            db.log_ml_event(
                                "model_rolled_back",
                                {"reverted_to_id": prev["id"], "rejected_id": run["id"],
                                 "regression_pct": round(-improved_pct, 2)},
                                f"new model rejected — {-improved_pct:.1f}% worse, "
                                f"reverted to previous champion",
                            )
                        else:
                            # Marginal change — keep new as challenger,
                            # old stays archived. Default 'champion' on
                            # the new row is fine since the previous loop
                            # already had old as 'champion' which we now
                            # demote to 'archived'.
                            db.update_ml_run_role(prev["id"], "archived")
                        # Sweeping cleanup: archive every persisted-as-
                        # champion row OTHER than the current `run`. This
                        # demotes any siblings the auto-sweep wrote with
                        # the historical default role='champion' bug
                        # before we changed candidates to insert as
                        # 'archived'. Idempotent — no-op once the table
                        # is clean.
                        if run.get("id"):
                            cleared = db.archive_other_champions(run["id"])
                            if cleared:
                                print(f"[champion-challenger] archived "
                                      f"{cleared} stale champion rows")
                    except Exception as exc:  # noqa: BLE001
                        print(f"[champion-challenger] promotion logic failed: {exc}")
                # Drift check on the freshly-trained model — alerts when
                # last week's MAE is materially worse than the trailing 4w.
                try:
                    drift = await asyncio.to_thread(ml_diagnostics.drift_check)
                    if drift.get("drifting"):
                        db.log_ml_event(
                            "drift_alert",
                            drift,
                            f"drift alert — last-week MAE {drift['last_week_mae']}° "
                            f"vs trailing-4w {drift['trailing_4w_mae']}° "
                            f"(ratio {drift['ratio']}×)",
                        )
                except Exception as exc:  # noqa: BLE001
                    print(f"[ml-retrain] drift check failed: {exc}")
                last_paired_count = cur_paired
        except Exception as exc:  # noqa: BLE001
            print(f"[ml-retrain] error: {exc}")
            db.log_ml_event("retrain_done", {"error": str(exc)},
                            f"retrain error: {exc}")
        # Subsequent cadence — wait a full interval before the next
        # sweep. (First-iteration delay is at the top of the loop.)
        await asyncio.sleep(max(0, ML_RETRAIN_INTERVAL_SECONDS - ML_RETRAIN_INITIAL_DELAY_SECONDS))


async def _ml_incremental_backfill_loop() -> None:
    """Pull the last 7 days of historical predictions + actuals each
    cycle (default daily). Idempotent — INSERT OR REPLACE just refreshes
    rows for days that were already pulled. The 7-day window catches up
    automatically if the loop missed a few days (e.g. server downtime)."""
    while True:
        # Initial offset: wait a few minutes so the manual full-history
        # backfill (if running on a fresh start) finishes first.
        await asyncio.sleep(300)
        try:
            today = datetime.now(timezone.utc).date()
            start = (today - timedelta(days=7)).isoformat()
            end = (today - timedelta(days=1)).isoformat()
            print(f"[ml-backfill] daily incremental: {start} → {end}")
            before_paired = db.count_paired_total()
            db.log_ml_event(
                "backfill_start",
                {"start": start, "end": end, "before_paired": before_paired},
                f"backfill started — pulling {start} → {end}",
            )
            async with httpx.AsyncClient() as client:
                await ml_backfill.run_backfill(start, end, _ml_backfill_progress)
            counts = db.historical_counts()
            after_paired = int(counts.get("paired") or 0)
            added = after_paired - before_paired
            print(f"[ml-backfill] tables now: {counts.get('paired')} paired rows")
            db.log_ml_event(
                "backfill_done",
                {"start": start, "end": end, "added": added,
                 "total_paired": after_paired},
                f"backfill done — +{added} new pairs ({after_paired:,} total)",
            )
            # Online learning: feed each new pair into the River model.
            # Cheap (~ms per learn_one) and lets the online side catch
            # regime shifts faster than the daily batch retrain.
            if added > 0 and ml_online.info().get("available"):
                try:
                    new_rows = db.list_training_data()[-added:]  # newest pairs
                    for row in new_rows:
                        target = row.get("actual_max_f")
                        if target is None:
                            continue
                        feats = {k: v for k, v in row.items()
                                 if k not in ("city", "target_date",
                                              "forecast_horizon_hours",
                                              "actual_max_f")}
                        ml_online.learn_one(feats, float(target))
                    info = ml_online.info()
                    db.log_ml_event(
                        "online_update",
                        {"new_updates": added, "total_updates": info["n_updates"]},
                        f"online learner: +{added} samples (total {info['n_updates']})",
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[ml-online] learn batch failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[ml-backfill] error: {exc}")
            db.log_ml_event("backfill_done", {"error": str(exc)},
                            f"backfill error: {exc}")
        # Sleep the configured interval BEFORE the next pull (the initial
        # 5-min wait above only runs once).
        await asyncio.sleep(max(1, ML_BACKFILL_INTERVAL_SECONDS - 300))


@app.on_event("startup")
async def _startup() -> None:
    db.init()
    # Restore the persisted auto-trade config (virtual balance, tier
    # tweaks, etc.) before the loops kick off so the first tick uses the
    # last known good state rather than env defaults.
    _load_auto_trade_state()
    # Validate auto-trader config up front. Doesn't HALT startup (we still
    # want HTTP / UI / MCP up so the user can see + fix things), but logs
    # loud so the issue isn't silent. Each tick re-validates and skips
    # while the issue persists.
    cfg_errors = _validate_auto_trade_cfg(_auto_trade_state)
    if cfg_errors:
        msg = "; ".join(cfg_errors)
        print(f"[auto-trader] WARNING: config invalid at startup: {msg}")
        print("[auto-trader] auto-trader ticks will skip until this is fixed via /api/auto-trade/config")
    # account_mode integrity check: persisted cached balance MUST equal
    # SUM(account_transactions.amount). If they drift (manual DB edits,
    # crash mid-write, file rollback) trust the ledger and refresh the
    # cache so the next tick sizes from the correct number.
    if _auto_trade_state.get("account_mode"):
        ledger_bal = db.account_balance()
        cached_bal = float(_auto_trade_state.get("account_balance") or 0.0)
        if abs(ledger_bal - cached_bal) > 0.01:
            print(f"[account] WARNING: cached balance ${cached_bal:.2f} disagrees "
                  f"with ledger ${ledger_bal:.2f} — trusting ledger and refreshing cache")
            try:
                db.log_ml_event(
                    "data_quality",
                    {"scope": "account", "cached": round(cached_bal, 2),
                     "ledger": round(ledger_bal, 2),
                     "delta": round(ledger_bal - cached_bal, 2)},
                    f"account balance drift: cached=${cached_bal:.2f} "
                    f"vs ledger=${ledger_bal:.2f}; refreshed cache",
                )
            except Exception:  # noqa: BLE001
                pass
        _auto_trade_state["account_balance"] = round(ledger_bal, 2)
        _persist_auto_trade_state()
        if ledger_bal <= 0 and db.account_transaction_count() == 0:
            print("[account] account_mode is ON with zero balance and no transactions — "
                  "make a deposit via POST /api/account/deposit to start trading")
    global _snapshot_task, _settlement_task, _auto_trader_task, _ml_retrain_task, _ml_incremental_backfill_task
    if not SNAPSHOT_LOOP_DISABLED and _snapshot_task is None:
        _snapshot_task = asyncio.create_task(_snapshot_loop())
    if not SETTLEMENT_LOOP_DISABLED and _settlement_task is None:
        _settlement_task = asyncio.create_task(_settlement_loop())
    # Always start the auto-trader loop. It checks _auto_trade_state['enabled']
    # on each iteration so users can toggle from the UI without restart.
    if _auto_trader_task is None:
        _auto_trader_task = asyncio.create_task(_auto_trader_loop())
    if not ML_RETRAIN_LOOP_DISABLED and _ml_retrain_task is None:
        _ml_retrain_task = asyncio.create_task(_ml_retrain_loop())
    if not ML_BACKFILL_LOOP_DISABLED and _ml_incremental_backfill_task is None:
        _ml_incremental_backfill_task = asyncio.create_task(_ml_incremental_backfill_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    for name in ("_snapshot_task", "_settlement_task", "_auto_trader_task",
                 "_ml_retrain_task", "_ml_incremental_backfill_task"):
        task = globals().get(name)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            globals()[name] = None


class SizeTierIn(BaseModel):
    min_edge_cents: float = Field(..., ge=0, le=100)
    # Either a percent of account equity (preferred — scales with balance)
    # or a fixed dollar amount (legacy override). At least one must be set.
    pct_of_balance: Optional[float] = Field(None, ge=0, le=1)
    size_usd: Optional[float] = Field(None, ge=0)


class AutoTradeConfigIn(BaseModel):
    enabled: Optional[bool] = None
    min_edge_cents: Optional[int] = Field(None, ge=0, le=100)
    max_usd: Optional[float] = Field(None, gt=0)
    max_pct_of_balance: Optional[float] = Field(None, ge=0, le=1)
    min_usd: Optional[float] = Field(None, ge=0)
    interval_seconds: Optional[int] = Field(None, ge=10, le=86400)
    max_bets_per_city_per_day: Optional[int] = Field(None, ge=1, le=20)
    min_model_pct: Optional[float] = Field(None, ge=0, le=1)
    min_kelly: Optional[float] = Field(None, ge=0, le=1)
    skip_entry_below_cents: Optional[int] = Field(None, ge=0, le=100)
    bet_sizing_mode: Optional[str] = Field(None, pattern="^(kelly|tiers)$")
    size_tiers: Optional[List[SizeTierIn]] = None
    require_peak_in_bracket: Optional[bool] = None
    prevent_overlapping_brackets: Optional[bool] = None
    min_balance_usd: Optional[float] = Field(None, ge=0)
    virtual_mode: Optional[bool] = None
    virtual_starting_balance: Optional[float] = Field(None, ge=0)
    virtual_current_balance: Optional[float] = Field(None, ge=0)


@app.get("/api/auto-trade/info")
def auto_trade_info():
    """Current runtime config + recent activity stats."""
    return {**_auto_trade_state}


@app.post("/api/auto-trade/config")
def auto_trade_config(c: AutoTradeConfigIn):
    """Update auto-trader config at runtime — takes effect on the next
    loop tick (no restart needed). Bankroll has been retired; sizing
    reads the live ledger via db.account_balance(), so deposits and
    withdrawals on /api/account/* are the only way to grow or shrink
    the trading account."""
    for k in ("enabled", "min_edge_cents", "max_usd",
              "max_pct_of_balance", "min_usd", "min_balance_usd",
              "interval_seconds", "max_bets_per_city_per_day",
              "min_model_pct", "min_kelly", "skip_entry_below_cents",
              "bet_sizing_mode",
              "require_peak_in_bracket", "prevent_overlapping_brackets",
              "virtual_mode", "virtual_starting_balance",
              "virtual_current_balance"):
        v = getattr(c, k)
        if v is not None:
            _auto_trade_state[k] = v
    if c.size_tiers is not None:
        # Persist as plain dicts (Pydantic models would JSON-roundtrip but
        # the rest of the code reads with dict.get). Drop unset fields so
        # the size_for_bet preference order (pct → usd → fallback) works.
        out = []
        for t in c.size_tiers:
            row = {"min_edge_cents": t.min_edge_cents}
            if t.pct_of_balance is not None: row["pct_of_balance"] = t.pct_of_balance
            if t.size_usd is not None:       row["size_usd"]       = t.size_usd
            out.append(row)
        _auto_trade_state["size_tiers"] = out
    errors = _validate_auto_trade_cfg(_auto_trade_state)
    if errors:
        # Don't accept config that would jam the trader.
        raise HTTPException(400, "; ".join(errors))
    _persist_auto_trade_state()
    return {**_auto_trade_state}


@app.post("/api/auto-trade/virtual/reset")
def virtual_reset():
    """Reset virtual_current_balance back to virtual_starting_balance.
    Doesn't touch real bets, settled history, or any other config knobs
    — purely a simulator restart. Persists to disk so the reset survives
    a server restart."""
    if not _auto_trade_state.get("virtual_mode"):
        raise HTTPException(
            400,
            "virtual_mode is off — toggle it on first via /api/auto-trade/config "
            "before resetting the simulated balance",
        )
    start = float(_auto_trade_state.get("virtual_starting_balance") or 0)
    _auto_trade_state["virtual_current_balance"] = round(start, 2)
    _auto_trade_state.pop("last_run_error", None)
    _persist_auto_trade_state()
    return {
        "virtual_starting_balance": start,
        "virtual_current_balance": _auto_trade_state["virtual_current_balance"],
        "reset": True,
    }


@app.post("/api/auto-trade/now")
async def auto_trade_now():
    """Manual one-shot trigger. Runs even when enabled=false — handy for
    testing config without flipping the loop on."""
    async with httpx.AsyncClient() as client:
        placed = await _auto_trade_tick(client)
    return {
        **_auto_trade_state,
        "placed": placed,
        "count": len(placed),
    }


# ── Virtual account endpoints ─────────────────────────────────────────

class _AccountAmount(BaseModel):
    amount: float = Field(..., gt=0, description="USD amount; must be > 0")
    note: Optional[str] = None


def _refresh_account_cache_after_tx(tx: Dict) -> Dict:
    _auto_trade_state["account_balance"] = round(float(tx["balance_after"]), 2)
    _persist_auto_trade_state()
    return {
        "balance": _auto_trade_state["account_balance"],
        "transaction": tx,
    }


@app.post("/api/account/deposit")
def account_deposit(body: _AccountAmount):
    """Add funds to the virtual account. Always succeeds (no upper cap).
    Returns the new balance and the inserted transaction row."""
    try:
        tx = db.account_deposit(body.amount, note=body.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _refresh_account_cache_after_tx(tx)


@app.post("/api/account/withdraw")
def account_withdraw(body: _AccountAmount):
    """Pull funds out of the virtual account. Refused with 400 if it
    would push the balance below $0."""
    try:
        tx = db.account_withdraw(body.amount, note=body.note)
    except db.InsufficientFundsError as exc:
        raise HTTPException(400, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _refresh_account_cache_after_tx(tx)


@app.get("/api/account/balance")
def account_balance_endpoint():
    """Authoritative balance read. Pulls from the ledger so the caller
    can never see a stale cached number, and also returns when the most
    recent transaction landed (useful for "last updated 2m ago" UIs)."""
    bal = db.account_balance()
    txs = db.list_account_transactions(limit=1)
    last_ts = txs[0]["created_at"] if txs else None
    # Refresh the cached cfg balance so subsequent /api/auto-trade/info
    # calls see the same number without an extra round trip.
    _auto_trade_state["account_balance"] = round(bal, 2)
    return {
        "balance": round(bal, 2),
        "last_updated": last_ts,
        "account_mode": bool(_auto_trade_state.get("account_mode")),
        "min_balance_usd": float(_auto_trade_state.get("min_balance_usd") or 0),
        "transaction_count": db.account_transaction_count(),
    }


@app.get("/api/account/transactions")
def account_transactions(limit: int = 50):
    """Most recent transactions, newest first. Default 50, capped at
    1000 for safety on the wire."""
    capped = max(1, min(int(limit or 50), 1000))
    return db.list_account_transactions(limit=capped)


@app.get("/api/account/history")
def account_history():
    """Time-ordered (created_at, balance_after) for the P&L page chart."""
    return db.account_balance_history()


class PlaceBetIn(BaseModel):
    city: str
    bracket_label: str
    bracket_lo: int
    bracket_hi: int
    side: str = Field(pattern="^(YES|NO)$")
    # `size` is now optional — when omitted (None) the server computes it
    # from the auto-trader's tier table using `edge_cents` and live equity,
    # so manual bets get the same fluid sizing the loop uses. Pass an
    # explicit positive value to override (legacy "I want exactly $X"
    # behavior; useful for ad-hoc sandbox bets).
    size: Optional[int] = Field(None, gt=0)
    entry_cents: int = Field(ge=0, le=100)
    # Optional: edge in cents (modelPct − kalshiPct) for tier lookup when
    # `size` is auto-computed. Frontend has this from /api/state's
    # brackets[*].edge so we don't have to round-trip to look it up.
    edge_cents: Optional[float] = Field(None, ge=-100, le=100)
    # Manual override of the peak-in-bracket guard. When True, the server
    # places the bet even if the model's predicted peak falls outside the
    # bracket's payout region. UI surfaces a warning + confirm; setting
    # this here is the API equivalent of clicking "place anyway".
    allow_misaligned: bool = False


def _close_at_for_bet(b: Dict) -> Optional[str]:
    """Kalshi market close = end-of-day on target_date in city local tz
    (e.g. NYC 2026-05-04 → 2026-05-04T23:59-04:00). Frontend renders
    countdown / formatted local from this ISO string."""
    target = b.get("target_date")
    if not target:
        return None
    city = next((c for c in CITIES if c["code"] == b.get("city")), None)
    if not city:
        return None
    tzname = CITY_TZ.get(city["tz"])
    try:
        d = datetime.fromisoformat(target)
        if ZoneInfo and tzname:
            d = d.replace(hour=23, minute=59, second=0, tzinfo=ZoneInfo(tzname))
        else:
            d = d.replace(hour=23, minute=59, second=0, tzinfo=timezone.utc)
        return d.isoformat()
    except ValueError:
        return None


def _bet_row_to_log(b: Dict) -> Dict:
    """Shape used by the Terminal "Your bet log" panel."""
    placed = time.strftime("%H:%M:%S", time.localtime(b["placed_at"] / 1000))
    return {
        "id": b["id"],
        "time": placed,
        "city": b["city"],
        "bracket": {
            "label": b["bracket_label"],
            "lo": b["bracket_lo"],
            "hi": b["bracket_hi"],
        },
        "side": b["side"],
        "size": b["size"],
        "entry": b["entry_cents"] if b["side"] == "YES" else 100 - b["entry_cents"],
        "status": b.get("status", "open"),
        "settledPl": b.get("settled_pl"),
        "targetDate": b.get("target_date"),
        "closeAt": _close_at_for_bet(b),
        "settledMaxF": b.get("settled_max_f"),
    }


def _bet_in_bracket(bet: Dict, actual_max: float) -> bool:
    """Single source of truth for "did the bet's bracket capture this max?"
    Honors the lower-tail / upper-tail label semantics so that ≤87°F
    means (-∞, 87] (not the 20° storage window of bracket_lo/bracket_hi)
    and ≥88°F means [88, +∞). Used by both live-status and settlement
    so the UI can never disagree with what actually settles."""
    label = bet.get("bracket_label", "") or ""
    lower_tail = label.startswith("≤")
    upper_tail = label.startswith("≥")
    eff_lo = float("-inf") if lower_tail else bet["bracket_lo"]
    eff_hi = float("inf") if upper_tail else bet["bracket_hi"]
    return eff_lo <= actual_max <= eff_hi


def _peak_fits_bracket(
    label: Optional[str],
    bracket_lo: Optional[float],
    bracket_hi: Optional[float],
    side: str,
    peak: Optional[float],
) -> bool:
    """For a YES bet, does the predicted peak fall inside the region the
    bracket actually pays out on?

      Range bracket (X-Y°F):  pays YES when lo ≤ actual ≤ hi  → peak ∈ [lo, hi]
      Lower tail   (≤X°F):    pays YES when actual ≤ hi       → peak ≤ hi
      Upper tail   (≥X°F):    pays YES when actual ≥ lo       → peak ≥ lo

    The earlier version exempted ALL tail brackets, which let `≤62°F`
    YES through when the model predicted 67°F (the model expected to
    LOSE that bet). For NO bets the logic inverts — manual /api/bets
    handles those; the auto-trader only places YES.

    Returns True if there's not enough info to judge (peak or bounds None).
    """
    if peak is None:
        return True
    label = (label or "")
    lower_tail = label.startswith("≤")
    upper_tail = label.startswith("≥")
    is_yes = (side or "YES").upper() == "YES"
    # NO bets: invert. NO on ≤X pays when actual > X → peak > X (i.e., NOT in [lo,hi]).
    if not is_yes:
        if lower_tail and bracket_hi is not None:
            return peak > bracket_hi
        if upper_tail and bracket_lo is not None:
            return peak < bracket_lo
        if bracket_lo is not None and bracket_hi is not None:
            return not (bracket_lo <= peak <= bracket_hi)
        return True
    # YES path
    if lower_tail and bracket_hi is not None:
        return peak <= bracket_hi
    if upper_tail and bracket_lo is not None:
        return peak >= bracket_lo
    if bracket_lo is not None and bracket_hi is not None:
        return bracket_lo <= peak <= bracket_hi
    return True


def _bet_live_status(
    bet: Dict,
    max_so_far: Optional[float],
    forecast_remainder_max: Optional[float],
    today_target_date: Optional[str] = None,
    market_closed: bool = False,
) -> Dict:
    """Decide whether the bet has already locked in a win/loss based on
    today's observed max + remaining forecast. Returns:
      status: 'locked_win' | 'locked_loss' | 'in_bracket' | 'pending'
              | 'awaiting_settlement' (bet is for a past or future date
              — today's weather is irrelevant to it)
      degreesFromBracket: signed °F distance (0 if currently inside)

    Locking rule: ONLY lock when the trading window has already closed
    (`market_closed=True`). Until then we show the live progress
    (in_bracket / pending) informationally but never declare a winner —
    even if the math currently looks certain, late-day spikes or a
    revised forecast could still flip the outcome.
    """
    # Don't apply today's weather to a bet whose settlement date isn't today.
    bet_target = bet.get("target_date")
    if bet_target and today_target_date and bet_target != today_target_date:
        return {"status": "awaiting_settlement", "degreesFromBracket": None}

    if max_so_far is None:
        return {"status": "pending", "degreesFromBracket": None}

    label = bet.get("bracket_label", "") or ""
    upper_tail = label.startswith("≥")
    lower_tail = label.startswith("≤")
    eff_lo = float("-inf") if lower_tail else bet["bracket_lo"]
    eff_hi = float("inf") if upper_tail else bet["bracket_hi"]
    yes_side = bet["side"] == "YES"
    in_bracket_now = _bet_in_bracket(bet, max_so_far)
    deg_below = round(eff_lo - max_so_far, 1) if (max_so_far < eff_lo and eff_lo != float("-inf")) else 0.0
    deg_above = round(max_so_far - eff_hi, 1) if (max_so_far > eff_hi and eff_hi != float("inf")) else 0.0
    deg = 0.0 if in_bracket_now else (deg_above if deg_above else -deg_below)

    # If the trading window is still open, never lock — only report the
    # current relationship to the bracket. Outcome can still flip.
    if not market_closed:
        if in_bracket_now:
            return {"status": "in_bracket", "degreesFromBracket": 0.0}
        return {"status": "pending", "degreesFromBracket": deg}

    # Market closed → safe to call the outcome based on the day's max.
    rem = forecast_remainder_max if forecast_remainder_max is not None else max_so_far
    can_exceed_hi = rem > eff_hi
    will_reach_lo = max(max_so_far, rem) >= eff_lo
    locked_in = in_bracket_now and not can_exceed_hi
    locked_out = (max_so_far > eff_hi) or (not will_reach_lo)
    if locked_in:
        return {
            "status": "locked_win" if yes_side else "locked_loss",
            "degreesFromBracket": 0.0,
        }
    if locked_out:
        return {
            "status": "locked_loss" if yes_side else "locked_win",
            "degreesFromBracket": deg,
        }
    if in_bracket_now:
        return {"status": "in_bracket", "degreesFromBracket": 0.0}
    return {"status": "pending", "degreesFromBracket": deg}


def _mark_bet_to_market(
    b: Dict,
    current_yes_pct: float,
    city_state: Optional[Dict] = None,
) -> Dict:
    """Return settlement binary outcomes for an open bet.
    ifWin  = net profit if the bet resolves in your favour (payout - stake).
    ifLose = net loss if it resolves against you (always -size, the full stake).
    """
    entry = b["entry_cents"] / 100      # fraction, e.g. 0.26
    size = b["size"]                    # USD risked
    contracts = size / entry            # number of $1-payout contracts bought
    if b["side"] == "YES":
        if_win = round((1 - entry) * contracts, 2)   # profit per contract × count
        if_lose = -size
    else:
        # NO bet: you bought (100-entry)¢ contracts that pay $1 if NO wins
        no_entry = 1 - entry
        no_contracts = size / no_entry
        if_win = round((1 - no_entry) * no_contracts, 2)
        if_lose = -size
    max_so_far = city_state.get("maxSoFarF") if city_state else None
    forecast_rem = city_state.get("forecastRemainderMaxF") if city_state else None
    data_date = city_state.get("dataDate") if city_state else None
    city_tz = (city_state.get("city") or {}).get("tz") if city_state else None
    # Has the trading window already closed for this bet? (closeAt = end of
    # day on target_date in city local tz). We only declare winners /
    # losers after the window shuts — until then it's pending/in_bracket.
    market_closed = False
    close_iso = _close_at_for_bet(b)
    if close_iso:
        try:
            close_dt = datetime.fromisoformat(close_iso)
            market_closed = datetime.now(close_dt.tzinfo) >= close_dt
        except (ValueError, TypeError):
            pass
    live = _bet_live_status(
        b, max_so_far, forecast_rem,
        today_target_date=data_date,
        market_closed=market_closed,
    )
    # actual_max_f from historical_actuals when target_date is in the past.
    # Lets the frontend render State 3 ("Official high … Missed by N°F")
    # even before Kalshi posts the official settlement — the IEM ASOS /
    # NWS CLI archive is usually posted hours before Kalshi marks the
    # market settled.
    actual_max_f = None
    target = b.get("target_date")
    if target and data_date and target < data_date:
        ha = db.get_historical_actual(b["city"], target)
        if ha:
            actual_max_f = ha.get("actual_max_f")
    return {
        "id": f"P{b['id']}",
        "city": b["city"],
        "cityTz": city_tz,
        "bracket": b["bracket_label"],
        "bracketLo": b.get("bracket_lo"),
        "bracketHi": b.get("bracket_hi"),
        "side": b["side"],
        "size": size,
        "entry": round(entry, 3),
        "current": round(current_yes_pct, 3),
        "ifWin": if_win,
        "ifLose": if_lose,
        "liveStatus": live["status"],
        "maxSoFarF": max_so_far if (data_date and b.get("target_date") == data_date) else None,
        "actualMaxF": actual_max_f,
        "degreesFromBracket": live["degreesFromBracket"],
        "targetDate": b.get("target_date"),
        "dataDate": data_date,
        "marketClosed": market_closed,
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "cities": [c["code"] for c in CITIES],
        "open_bets": len(db.list_open_bets()),
    }


@app.post("/api/bets")
def place_bet(b: PlaceBetIn):
    city_code = b.city.upper()
    city = next((c for c in CITIES if c["code"] == city_code), None)
    target_date = _city_target_date(city) if city else None
    # Bracket-alignment guard: same logic the auto-trader applies. Returns
    # 400 if the predicted peak doesn't fall in the bracket's payout
    # region, unless the caller explicitly opts in with allow_misaligned.
    # Reads model_max from the most recent persisted snapshot for the
    # city (sync — no async loop needed). Falls open if no snapshot yet.
    if _auto_trade_state.get("require_peak_in_bracket", True) and not b.allow_misaligned:
        try:
            snap = db.latest_snapshot(city_code)
            peak = snap.get("model_max") if snap else None
            if peak is not None and not _peak_fits_bracket(
                b.bracket_label, b.bracket_lo, b.bracket_hi, b.side, float(peak),
            ):
                raise HTTPException(
                    400,
                    f"bracket misaligned with model — predicted peak {float(peak):.1f}°F "
                    f"falls outside the {b.bracket_label} {b.side} payout region. "
                    "Pass allow_misaligned=true to override.",
                )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[place-bet] alignment check skipped: {exc}")
    # Resolve final stake. When `size` is omitted, route through the same
    # tier-sizing path the auto-trader uses so manual placements scale
    # with the live account too — fixes the "manual bets default to
    # $50 regardless of edge" pattern surfaced in the bet-by-bet review.
    cfg = _auto_trade_state
    size = b.size
    if size is None:
        # Same sizing source the auto-trader uses: live account balance
        # from the ledger. Bankroll is gone, so deposits / withdrawals
        # via /api/account/* are the only way to grow the deployable pot.
        balance = db.account_balance()
        # Edge fallback: if frontend didn't pass it, treat as 0 — Kelly
        # fraction will be tiny and tier lookup will fall through to the
        # min_usd floor. Better to under-size than to silently $0-out.
        edge_cents = float(b.edge_cents or 0.0)
        edge_frac = edge_cents / 100.0
        # kalshi_pct ≈ entry_cents / 100. Use that for the kelly arg.
        kalshi_pct = (b.entry_cents or 50) / 100.0
        kelly = kelly_fraction(edge_frac, kalshi_pct)
        size = _size_for_bet(cfg, edge_cents, kelly, balance, total_equity=balance)
        if size <= 0:
            raise HTTPException(
                400,
                "auto-sized to $0 — account balance too low or edge below tier thresholds. "
                "Deposit via /api/account/deposit or pass an explicit `size`.",
            )
    row = db.insert_bet(
        city=city_code,
        bracket_label=b.bracket_label,
        bracket_lo=b.bracket_lo,
        bracket_hi=b.bracket_hi,
        side=b.side,
        size=int(size),
        entry_cents=b.entry_cents,
        target_date=target_date,
    )
    # account_mode: debit the stake from the ledger. Manual placements
    # are funded from the same account as the auto-trader; failure rolls
    # the bet back. Falls through to virtual_mode for legacy setups.
    if _auto_trade_state.get("account_mode"):
        try:
            db.account_record_bet_placed(
                row["id"], int(size),
                min_balance=float(_auto_trade_state.get("min_balance_usd") or 0),
            )
        except db.InsufficientFundsError as exc:
            db.delete_bet(row["id"])
            raise HTTPException(400, str(exc))
        _auto_trade_state["account_balance"] = round(db.account_balance(), 2)
        _persist_auto_trade_state()
    elif _auto_trade_state.get("virtual_mode"):
        # Mirror the auto-trader's virtual-mode debit for manual placements
        # so the simulated bankroll tracks ALL bets, not just the auto ones.
        _auto_trade_state["virtual_current_balance"] = round(
            float(_auto_trade_state.get("virtual_current_balance") or 0) - int(size), 2,
        )
        _persist_auto_trade_state()
    return _bet_row_to_log(row)


@app.get("/api/bets")
def get_bets(limit: int = 200):
    return [_bet_row_to_log(b) for b in db.list_bets(limit)]


async def _settle_open_bets(client: httpx.AsyncClient) -> List[Dict]:
    """Settle every open bet whose city-local target_date has passed.

    Strategy: bucket settleable bets by city, fetch each office's CLI
    once, parse the YESTERDAY MAX, then apply realized P/L per bet.
    Bets whose target_date doesn't match the CLI's coverage day are
    deferred to a later run."""
    settled: List[Dict] = []
    by_city: Dict[str, List[Dict]] = {}
    for c in CITIES:
        today = _city_today(c["tz"])
        for bet in db.list_settleable_bets(today):
            if bet["city"] == c["code"]:
                by_city.setdefault(c["code"], []).append(bet)

    for city in CITIES:
        bets = by_city.get(city["code"], [])
        if not bets:
            continue
        text = await fetch_climate_report(client, city["office"])
        actual_max = parse_climate_max_yesterday(text)
        if actual_max is None:
            continue  # CLI not yet posted, try again next loop iteration
        # CLI's YESTERDAY column is for the city-local previous day.
        cli_covers = (
            datetime.fromisoformat(_city_today(city["tz"])) - timedelta(days=1)
        ).date().isoformat()
        for bet in bets:
            if bet["target_date"] != cli_covers:
                continue  # CLI is for a different day than this bet
            in_bracket = _bet_in_bracket(bet, actual_max)
            pl = settle_pl(bet["side"], bet["entry_cents"], bet["size"], in_bracket)
            db.settle_bet(bet["id"], pl, actual_max)
            # account_mode: only bets we actually funded out of the
            # ledger touch the ledger on settle. Bets placed before
            # account_mode was enabled stay invisible to the account.
            if db.account_bet_was_funded(bet["id"]):
                stake = float(bet["size"])
                if pl > 0:
                    gross_payout = stake + float(pl)
                    db.account_record_bet_won(bet["id"], gross_payout)
                    _auto_trade_state["account_balance"] = round(db.account_balance(), 2)
                    _persist_auto_trade_state()
                    print(f"[auto-trader] [account] settled WIN bet#{bet['id']} "
                          f"+${gross_payout:.2f} → balance="
                          f"${_auto_trade_state['account_balance']:.2f}")
                else:
                    db.account_record_bet_lost(bet["id"])
                    print(f"[auto-trader] [account] settled LOSS bet#{bet['id']} "
                          f"stake ${stake:.2f} already debited at placement")
            # Virtual mode (legacy, only relevant when account_mode is off
            # and the bet wasn't account-funded): credit gross payout on win.
            elif _auto_trade_state.get("virtual_mode") and pl > 0:
                stake = float(bet["size"])
                gross_payout = stake + float(pl)
                _auto_trade_state["virtual_current_balance"] = round(
                    float(_auto_trade_state.get("virtual_current_balance") or 0) + gross_payout, 2,
                )
                _persist_auto_trade_state()
                print(f"[auto-trader] [virtual] settled WIN bet#{bet['id']} "
                      f"+${gross_payout:.2f} → balance="
                      f"${_auto_trade_state['virtual_current_balance']:.2f}")
            # Data-quality cross-check: NWS Climate Report's actual_max
            # should match the IEM ASOS hourly archive value we
            # backfilled into historical_actuals. If they disagree by
            # more than 0.5°F, log a data_quality event so the user
            # knows there's a discrepancy worth investigating.
            try:
                ha = db.get_historical_actual(bet["city"], cli_covers)
                if ha is not None and ha.get("actual_max_f") is not None:
                    iem = float(ha["actual_max_f"])
                    if abs(iem - actual_max) > 0.5:
                        db.log_ml_event(
                            "data_quality",
                            {"city": bet["city"], "date": cli_covers,
                             "nws_climate": actual_max, "iem_asos": iem,
                             "delta": round(iem - actual_max, 2)},
                            f"data quality: {bet['city']} {cli_covers} "
                            f"NWS={actual_max}° vs IEM={iem}° (Δ{iem - actual_max:+.1f}°)",
                        )
            except Exception as exc:  # noqa: BLE001
                print(f"[settle] data_quality check failed: {exc}")
            settled.append({
                "id": bet["id"],
                "city": bet["city"],
                "bracket": bet["bracket_label"],
                "side": bet["side"],
                "actual_max_f": actual_max,
                "in_bracket": in_bracket,
                "pl": round(pl, 2),
            })
    return settled


async def _settlement_loop() -> None:
    """Background settlement: fires hourly, idempotent. CLI bulletins for a
    given day are typically out by mid-morning local time, so an hourly
    cadence catches them within an hour of availability."""
    while True:
        try:
            async with httpx.AsyncClient() as client:
                await _settle_open_bets(client)
        except Exception as exc:  # noqa: BLE001
            print(f"[bets] settlement loop error: {exc}")
        await asyncio.sleep(SETTLEMENT_INTERVAL_SECONDS)


# Where the persisted auto-trade state lives — only the bits that need
# to survive restarts (virtual balance + the user-tunable knobs the
# config endpoint touches). Counters (last_run_*, total_*) are session-
# scoped on purpose.
_AUTO_TRADE_STATE_FILE = Path(__file__).parent / "auto_trade_state.json"
_AUTO_TRADE_PERSIST_KEYS = (
    "enabled", "min_edge_cents", "max_usd", "max_pct_of_balance",
    "min_usd", "max_bets_per_city_per_day", "min_model_pct", "min_kelly",
    "skip_entry_below_cents", "interval_seconds",
    "require_peak_in_bracket", "prevent_overlapping_brackets",
    "min_balance_usd", "bet_sizing_mode", "size_tiers",
    "virtual_mode", "virtual_starting_balance", "virtual_current_balance",
    "account_mode", "account_balance",
)


def _persist_auto_trade_state() -> None:
    """Write the user-tunable subset of _auto_trade_state to disk.
    Called after every config change AND after every virtual-balance
    update so a kill -9 doesn't lose the simulated bankroll."""
    try:
        snapshot = {k: _auto_trade_state.get(k) for k in _AUTO_TRADE_PERSIST_KEYS}
        tmp = _AUTO_TRADE_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, indent=2))
        tmp.replace(_AUTO_TRADE_STATE_FILE)
    except Exception as exc:  # noqa: BLE001
        print(f"[auto-trader] persist failed: {exc}")


def _load_auto_trade_state() -> None:
    """Read the persisted state on startup. Missing file or unreadable
    JSON falls back to env defaults (no-op). Called from _startup."""
    if not _AUTO_TRADE_STATE_FILE.exists():
        return
    try:
        loaded = json.loads(_AUTO_TRADE_STATE_FILE.read_text())
        for k in _AUTO_TRADE_PERSIST_KEYS:
            if k in loaded and loaded[k] is not None:
                _auto_trade_state[k] = loaded[k]
        print(f"[auto-trader] loaded persisted state from {_AUTO_TRADE_STATE_FILE.name}: "
              f"virtual_mode={_auto_trade_state.get('virtual_mode')} "
              f"virtual_balance={_auto_trade_state.get('virtual_current_balance')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[auto-trader] load failed (using defaults): {exc}")


def _sizing_balance(cfg: Dict, total_equity: float, available_cash: float) -> tuple:
    """Pick the (equity, cash) pair the sizer should use.

    account_mode (default) → both come from db.account_balance(). The
    ledger debits the stake at placement and credits the gross payout at
    settlement, so one number is simultaneously "what's in the account"
    and "what's deployable." The persisted cfg["account_balance"] mirrors
    the ledger for fast read paths and is reconciled on startup.

    virtual_mode → both come from virtual_current_balance (legacy paper-
    trading sandbox; preserved for A/B comparisons).

    Otherwise → fall back to the ledger too. Bankroll has been retired,
    so there is no separate "real mode" sizing source — the ledger IS
    the bank account."""
    if cfg.get("account_mode"):
        bal = db.account_balance()
        cfg["account_balance"] = round(bal, 2)
        return bal, bal
    if cfg.get("virtual_mode"):
        v = float(cfg.get("virtual_current_balance") or 0)
        return v, v
    bal = db.account_balance()
    return bal, bal


def _validate_auto_trade_cfg(cfg: Dict) -> List[str]:
    """Sanity-check the auto-trader config so a bad knob can't silently
    misbehave. Returns a list of human-readable error strings; empty list
    means OK. Called at startup AND before every tick.

    Invariants:
      - min_usd > 0
      - min_usd < max_usd  (otherwise sizing is clamped to a single value)
      - max_pct_of_balance ∈ [0, 1]
    """
    errors: List[str] = []
    min_usd = float(cfg.get("min_usd") or 0)
    max_usd = float(cfg.get("max_usd") or 0)
    max_pct = cfg.get("max_pct_of_balance")
    if min_usd <= 0:
        errors.append(f"min_usd ({min_usd}) must be > 0")
    if max_usd <= 0:
        errors.append(f"max_usd ({max_usd}) must be > 0")
    if min_usd > 0 and max_usd > 0 and min_usd >= max_usd:
        errors.append(
            f"min_usd ({min_usd}) must be < max_usd ({max_usd}) — they currently "
            f"clamp every bet to a single value"
        )
    if max_pct is not None:
        try:
            mp = float(max_pct)
            if mp < 0 or mp > 1:
                errors.append(f"max_pct_of_balance ({mp}) must be in [0, 1]")
        except (TypeError, ValueError):
            errors.append(f"max_pct_of_balance ({max_pct!r}) must be numeric")
    return errors


def _size_for_bet(
    cfg: Dict,
    edge_cents: float,
    kelly: float,
    available_cash: float,
    total_equity: Optional[float] = None,
) -> int:
    """Pick the dollar stake for a bet. Returns 0 if cfg blocks placement.

    Honors cfg['bet_sizing_mode']:

      - 'tiers': scan size_tiers (highest min_edge_cents first); the first
                 matching tier picks the stake. Tier defines the stake as a
                 PERCENT of total_equity (pct_of_balance), so the dollar
                 amount scales fluidly as the account grows or shrinks.
                 (Legacy size_usd field still honored if present, treated
                 as a fixed dollar override on that tier.)
                 Falls through to kelly if nothing matches.
      - 'kelly' (or anything else): kelly_fraction × available_cash.

    The result is always clamped to [min_usd, min(max_usd, available_cash)].
    """
    min_usd = float(cfg.get("min_usd") or 0)
    # Effective max = min(absolute $ cap, percent-of-equity cap, available
    # cash). The percent cap auto-scales with balance; the absolute cap is
    # a safety belt; available cash is the unconditional ceiling (we can't
    # bet money that's already in flight).
    abs_max = float(cfg.get("max_usd") or available_cash)
    pct_cap = cfg.get("max_pct_of_balance")
    pct_max = float(pct_cap) * (float(total_equity) if total_equity is not None else available_cash) \
              if pct_cap is not None else abs_max
    cap = max(0.0, min(abs_max, pct_max, available_cash))
    # Default total_equity to available_cash if the caller didn't supply
    # the headline-equity number — keeps unit tests / older callers working.
    equity = float(total_equity) if total_equity is not None else float(available_cash)
    raw = 0.0
    mode = (cfg.get("bet_sizing_mode") or "kelly").lower()
    if mode == "tiers":
        tiers = sorted(
            cfg.get("size_tiers") or [],
            key=lambda t: -float(t.get("min_edge_cents") or 0),
        )
        for tier in tiers:
            if edge_cents >= float(tier.get("min_edge_cents") or 0):
                # Prefer a percent-of-balance spec; fall back to an
                # explicit dollar amount for back-compat with old tiers.
                pct = tier.get("pct_of_balance")
                if pct is not None:
                    raw = float(pct) * equity
                elif tier.get("size_usd") is not None:
                    raw = float(tier["size_usd"])
                break
        # No tier matched — fall through to kelly so we still scale
        # gracefully near the lowest threshold.
        if raw <= 0:
            raw = kelly * available_cash
    else:
        raw = kelly * available_cash
    raw = max(min_usd, raw)
    sized = int(min(cap, raw))
    return sized if sized >= min_usd else 0


async def _auto_trade_tick(client: httpx.AsyncClient) -> List[Dict]:
    """One iteration of the auto-trader. Considers every bracket on every
    city's ladder (not just the top-recommended one) — so once we've bet
    the headline bracket, subsequent ticks can still find positive edge
    on neighboring brackets and keep firing. Picks the SINGLE highest-edge
    eligible bracket across all (city, bracket) pairs and places a
    ¼-Kelly YES bet on it. Idempotent across ticks AND restarts via
    list_bets_for_target."""
    cfg = _auto_trade_state
    # Pre-tick config validation. If something's wrong (min_usd >= max_usd,
    # min_balance >= bankroll, etc.) halt the tick rather than silently
    # placing degenerate bets.
    cfg_errors = _validate_auto_trade_cfg(cfg)
    if cfg_errors:
        msg = "; ".join(cfg_errors)
        print(f"[auto-trader] config invalid — skipping tick: {msg}")
        try:
            db.log_ml_event(
                "data_quality",
                {"scope": "auto-trader", "errors": cfg_errors},
                f"auto-trader config invalid: {msg}",
            )
        except Exception:  # noqa: BLE001
            pass
        cfg["last_run_ts"] = int(time.time())
        cfg["last_run_placed"] = 0
        cfg["last_run_considered"] = 0
        cfg["last_run_error"] = msg
        cfg["total_runs"] += 1
        return []
    cfg.pop("last_run_error", None)
    max_per_day = int(cfg.get("max_bets_per_city_per_day") or 2)
    candidates: List[tuple] = []  # (edge_cents, city, state, bracket, target)

    # Probe cities in parallel; one slow upstream shouldn't stall the
    # whole tick. asyncio.gather with return_exceptions so a single
    # failure is logged, not propagated.
    results = await asyncio.gather(
        *[_build_state_cached(c, client) for c in CITIES],
        return_exceptions=True,
    )
    for city, state in zip(CITIES, results):
        if isinstance(state, Exception):
            print(f"[auto-trader] probe error on {city['code']}: {state}")
            continue
        if state is None:
            continue
        target = state.get("targetDate")
        if target is None:
            continue
        existing = db.list_bets_for_target(city["code"], target)
        existing_labels = {b["bracket_label"] for b in existing}
        # Per-city-per-day cap — skip the whole city once we've already
        # opened max_per_day bets for this target_date.
        if len(existing) >= max_per_day:
            continue
        # Active prediction the auto-trader is using right now (post bias
        # correction, blend, etc.). Used by the peak-in-bracket gate.
        active_max = state.get("activeMax") or state.get("modelMax")
        require_peak = bool(cfg.get("require_peak_in_bracket", True))
        prevent_overlap = bool(cfg.get("prevent_overlapping_brackets", True))
        for bracket in state.get("brackets") or []:
            edge_cents = int(round((bracket.get("edge") or 0) * 100))
            if edge_cents < cfg["min_edge_cents"]:
                continue
            if bracket.get("label") in existing_labels:
                continue
            label = bracket.get("label", "") or ""
            blo, bhi = bracket.get("lo"), bracket.get("hi")
            # Bracket-alignment gate: refuse to bet on a bracket whose
            # payout region doesn't contain the predicted peak. Auto-trader
            # only places YES — _peak_fits_bracket handles range brackets
            # AND the directional check on tails (≤X needs peak ≤ X, ≥X
            # needs peak ≥ X). Earlier version exempted all tails, which
            # is wrong: a ≤62°F YES with a 67°F prediction is structurally
            # a losing bet (model expects to lose).
            if require_peak and active_max is not None:
                if not _peak_fits_bracket(label, blo, bhi, "YES", active_max):
                    continue
            # Overlap gate: refuse to layer a candidate over a position we
            # already hold for this (city, target_date). Same-day adjacent
            # brackets are mutually exclusive — extra exposure, no upside.
            if prevent_overlap and blo is not None and bhi is not None:
                clash = False
                for ex in existing:
                    elo, ehi = ex.get("bracket_lo"), ex.get("bracket_hi")
                    if elo is None or ehi is None:
                        continue
                    if blo <= ehi and elo <= bhi:  # closed-interval overlap
                        clash = True
                        break
                if clash:
                    continue
            # Quality gate — drop garbage long-shots even when they
            # show "edge" because a small probability error compounds
            # into a near-guaranteed loss when entry is tiny.
            model_pct = bracket.get("modelPct") or 0
            yes_cents = bracket.get("yesPrice") or 0
            if model_pct < cfg.get("min_model_pct", 0.12):
                continue
            if yes_cents < cfg.get("skip_entry_below_cents", 5):
                continue
            k = kelly_fraction(bracket.get("edge") or 0, bracket.get("kalshiPct") or 0.5)
            if k < cfg.get("min_kelly", 0.005):
                continue
            candidates.append((edge_cents, city, state, bracket, target))

    # Rolling bank-account math: starting_cash + realized_pl − open stakes.
    # Money leaves the account when a bet is placed and returns + profit
    # when it wins. We sit out a tick if available cash is below min_usd.
    # Bankroll is gone — equity == account balance from the ledger. We
    # still surface realized_pl / open_stakes for the UI so the auto-trade
    # info card can show "where the money has flowed."
    stats = db.stats_summary()
    ledger_balance = db.account_balance()
    cfg["available_cash"] = round(ledger_balance, 2)
    cfg["total_equity"] = round(ledger_balance, 2)
    cfg["realized_pl"] = stats["realizedPl"]
    cfg["open_stakes"] = stats["openStakes"]
    # Pick the balance the sizer should see. account_mode → ledger;
    # virtual_mode → simulated balance; otherwise → ledger as well, since
    # bankroll is retired.
    total_equity, available_cash = _sizing_balance(cfg, ledger_balance, ledger_balance)
    # Halt the entire run if the funding source is below the floor —
    # account_mode and virtual_mode both gate on the same min_balance_usd.
    if cfg.get("account_mode") or cfg.get("virtual_mode"):
        floor = float(cfg.get("min_balance_usd") or 0)
        if available_cash <= floor:
            mode = "account" if cfg.get("account_mode") else "virtual"
            print(f"[auto-trader] {mode} balance exhausted: "
                  f"${available_cash:.2f} <= min_balance=${floor:.2f}")
            try:
                db.log_ml_event(
                    "data_quality",
                    {"scope": "auto-trader", "mode": mode,
                     "balance": round(available_cash, 2),
                     "min_balance_usd": floor},
                    f"{mode} balance exhausted (${available_cash:.2f}); "
                    f"deposit via /api/account/deposit to resume",
                )
            except Exception:  # noqa: BLE001
                pass
            cfg["last_run_ts"] = int(time.time())
            cfg["last_run_placed"] = 0
            cfg["last_run_considered"] = len(candidates)
            cfg["last_run_floor_skips"] = 0
            cfg["last_run_error"] = f"{mode}_balance_exhausted"
            cfg["total_runs"] += 1
            return []

    placed: List[Dict] = []
    min_balance = float(cfg.get("min_balance_usd") or 0)
    floor_skips = 0   # # of candidates we passed up because they'd breach the floor
    if candidates and available_cash >= cfg["min_usd"]:
        candidates.sort(key=lambda c: -c[0])
        # Walk candidates highest-edge first; place the first one whose
        # size fits the stop-loss floor. The previous "pick top, place or
        # nothing" rule meant a single oversized candidate would silently
        # block a smaller-but-still-good fallback.
        for cand_idx, cand in enumerate(candidates):
            edge_cents, city, state, bracket, target = cand
            kelly = kelly_fraction(bracket.get("edge") or 0, bracket.get("kalshiPct") or 0.5)
            size = _size_for_bet(cfg, edge_cents, kelly, available_cash, total_equity=total_equity)
            if size < cfg["min_usd"]:
                continue
            # Stop-loss floor — refuse a bet that would drop deployable
            # cash below min_balance_usd. Try the next-best candidate
            # (which may need a smaller stake by tier) instead of bailing.
            if available_cash - size < min_balance:
                floor_skips += 1
                print(f"[auto-trader] floor-skip {city['code']} {bracket['label']} "
                      f"size=${size} would leave cash=${available_cash - size:.2f} "
                      f"< min_balance=${min_balance}")
                continue
            try:
                row = db.insert_bet(
                    city=city["code"],
                    bracket_label=bracket["label"],
                    bracket_lo=bracket["lo"],
                    bracket_hi=bracket["hi"],
                    side="YES",
                    size=size,
                    entry_cents=int(bracket.get("yesPrice", 0)),
                    target_date=target,
                )
                # account_mode: debit the stake through the ledger so
                # /api/account/balance and the chart see the new state
                # immediately. The ledger is the source of truth; cfg is
                # a cache. If the debit fails (e.g. concurrent withdrawal
                # raced us below the floor) roll the bet back to keep
                # the ledger / bets table consistent.
                if cfg.get("account_mode"):
                    try:
                        db.account_record_bet_placed(
                            row["id"], size,
                            min_balance=float(cfg.get("min_balance_usd") or 0),
                        )
                    except db.InsufficientFundsError as exc:
                        print(f"[auto-trader] ledger refused stake for "
                              f"bet#{row['id']}: {exc} — rolling back")
                        db.delete_bet(row["id"])
                        floor_skips += 1
                        continue
                    cfg["account_balance"] = round(db.account_balance(), 2)
                    available_cash = cfg["account_balance"]
                    _persist_auto_trade_state()
                # Virtual mode (legacy, ignored when account_mode is on):
                # debit stake immediately and persist.
                elif cfg.get("virtual_mode"):
                    cfg["virtual_current_balance"] = round(
                        float(cfg.get("virtual_current_balance") or 0) - size, 2
                    )
                    available_cash -= size  # so subsequent candidates this tick see the lower balance
                    _persist_auto_trade_state()
                placed.append({
                    "id": row["id"],
                    "city": city["code"],
                    "bracket": bracket["label"],
                    "edge_cents": edge_cents,
                    "size_usd": size,
                    "entry_cents": row["entry_cents"],
                    "target_date": target,
                    "ts": int(time.time()),
                    "considered": len(candidates),
                })
                print(f"[auto-trader] picked {city['code']} {bracket['label']} "
                      f"(+{edge_cents}¢ edge, beat {cand_idx} above) — "
                      f"size=${size} entry={row['entry_cents']}¢"
                      f"{' [virtual]' if cfg.get('virtual_mode') else ''}")
                break  # one bet per tick, by design
            except Exception as exc:  # noqa: BLE001
                print(f"[auto-trader] insert error: {exc}")
                continue
        # If we walked the entire list and EVERY candidate would have
        # breached the floor, surface that loud — usually means the
        # account is over-exposed and the trader is correctly idle.
        if not placed and floor_skips > 0 and floor_skips == len(candidates):
            try:
                db.log_ml_event(
                    "data_quality",
                    {"scope": "auto-trader", "available_cash": round(available_cash, 2),
                     "min_balance_usd": min_balance, "floor_skips": floor_skips},
                    f"auto-trader paused — every candidate ({floor_skips}) would "
                    f"breach min_balance_usd=${min_balance:.0f} (cash=${available_cash:.2f})",
                )
            except Exception:  # noqa: BLE001
                pass

    cfg["last_run_ts"] = int(time.time())
    cfg["last_run_placed"] = len(placed)
    cfg["last_run_considered"] = len(candidates)
    cfg["last_run_floor_skips"] = floor_skips
    cfg["total_runs"] += 1
    cfg["total_placed"] += len(placed)
    return placed


async def _auto_trader_loop() -> None:
    """Background auto-trader. Always runs (so UI toggles take effect
    without restart) — checks the enabled flag inside each iteration.
    Sleep duration also reads from state so cadence changes via
    /api/auto-trade/config take effect on the next iteration."""
    print(f"[auto-trader] loop active — initial state={_auto_trade_state}")
    while True:
        try:
            if _auto_trade_state["enabled"]:
                async with httpx.AsyncClient() as client:
                    await _auto_trade_tick(client)
        except Exception as exc:  # noqa: BLE001
            print(f"[auto-trader] loop error: {exc}")
        # Re-read interval each iteration so config changes apply.
        await asyncio.sleep(max(10, int(_auto_trade_state.get("interval_seconds") or 600)))


@app.post("/api/settle")
async def settle_now():
    """Manual trigger for the settlement job. Useful for testing and for
    forcing a check after the morning CLI is known to have posted."""
    async with httpx.AsyncClient() as client:
        results = await _settle_open_bets(client)
    return {"settled": results, "count": len(results)}


@app.post("/api/settle/recompute")
def recompute_settled_pl():
    """Re-run settle_pl on every already-settled bet to repair P/L values
    that were computed with the old (broken) formula. Idempotent — running
    again on already-correct rows is a no-op since the math is fixed."""
    fixed = 0
    skipped = 0
    for b in db.list_bets(limit=10_000):
        if b.get("status") != "settled" or b.get("settled_max_f") is None:
            skipped += 1
            continue
        in_bracket = _bet_in_bracket(b, b["settled_max_f"])
        new_pl = settle_pl(b["side"], b["entry_cents"], b["size"], in_bracket)
        if abs(new_pl - (b.get("settled_pl") or 0)) > 0.01:
            db.settle_bet(b["id"], new_pl, b["settled_max_f"])
            fixed += 1
    return {"fixed": fixed, "skipped": skipped}


@app.get("/api/stats")
def get_stats():
    return db.stats_summary()


@app.post("/api/ml/runs/normalize-champions")
def ml_normalize_champions():
    """One-shot cleanup: keep only the single most-recent persisted
    `champion` row, archive all other champions. Fixes the historical
    drift where every algo in every sweep got marked 'champion' by the
    insert default. Idempotent — safe to call repeatedly."""
    latest = db.latest_ml_run()
    if not latest:
        return {"archived": 0, "champion_id": None}
    archived = db.archive_other_champions(latest["id"])
    return {
        "champion_id": latest["id"],
        "champion_algorithm": latest["algorithm"],
        "champion_test_mae": latest.get("test_mae"),
        "champion_walk_forward_mae": latest.get("walk_forward_mae"),
        "archived": archived,
    }


@app.post("/api/ml/recompute-ensemble")
def ml_recompute_ensemble():
    """Recompute every historical_predictions.ensemble_max using the LIVE
    blend formula (model.live_style_ensemble) so training data sees the
    same ensemble pipeline that drives live inference. Closes the
    train/inference covariate shift on the ensemble_max feature.

    Idempotent and read-most/write-some: rows whose recomputed value
    matches the stored value are skipped. After running this, also
    POST /api/ml/train to refit the model on the aligned data, then
    /api/ml/replay-historical to refresh the bias map with the new
    predictions.

    Returns {updated, cleared, skipped, total} so the caller can see how
    many rows actually changed.
    """
    return db.recompute_historical_ensemble_max()


@app.post("/api/ml/replay-historical")
def ml_replay_historical(limit: Optional[int] = None):
    """Run the active ML model against every historical_predictions row
    that has a paired actual, and persist the predictions to
    ml_historical_predictions. The ML-bias map then UNIONs both real
    snapshots and these synthetic ones, so corrections stabilize from
    a few days of live capture to weeks/months of backfilled coverage.

    Idempotent — the upsert overwrites existing rows by
    (city, target_date, forecast_horizon_hours), so re-running after a
    retrain refreshes every predicted point with the new model.

    Optional `limit` query param caps how many rows to process per call
    (useful for incremental backfills on big DBs). Default = unlimited.
    """
    # Reuse the same training-data join the trainer uses; it has all the
    # lag/seasonal features pre-computed via the SQL window functions
    # so we don't have to re-derive them here.
    rows = db.list_training_data() or []
    if not rows:
        return {"replayed": 0, "skipped": 0, "reason": "no historical rows with actuals"}
    if limit is not None and limit > 0:
        rows = rows[-int(limit):]   # most recent N — usually what you want

    replayed = 0
    skipped = 0
    failed = 0
    for r in rows:
        city = r.get("city")
        target_date = r.get("target_date")
        horizon = int(r.get("forecast_horizon_hours") or 24)
        if not city or not target_date:
            skipped += 1
            continue
        # Build the feature dict the model expects. Most fields come
        # straight off the row; the trainer's `_engineer_features` will
        # handle derived columns (spreads, doy_sin/cos, etc.) at predict
        # time inside _predict_with's featurize step.
        features = {k: r.get(k) for k in (
            "gfs_max", "ecmwf_max", "icon_max", "om_max",
            "gfs_mos_max", "nam_mos_max",
            "t850_c", "t700_c", "t500_c", "h500_m", "rh850_pct",
            "ensemble_max",
            "prev_actual_max_f", "lag3_actual_max_f", "lag7_actual_max_f",
            "roll7_mean_max_f", "seasonal_avg_max_f",
            "forecast_horizon_hours",
        )}
        # target_date is needed by _predict_with's _engineer_features
        # for doy_sin/doy_cos. The training join already exposes it.
        features["target_date"] = target_date
        try:
            ml_max = ml_predict.predict_max(features, city=city)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if failed <= 3:
                print(f"[ml-replay] predict failed on {city} {target_date}: {exc}")
            continue
        if ml_max is None:
            skipped += 1
            continue
        try:
            db.upsert_ml_replay_row(city, target_date, horizon, float(ml_max))
            replayed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if failed <= 3:
                print(f"[ml-replay] upsert failed on {city} {target_date}: {exc}")
    # Invalidate the ML bias cache so the next state build picks up the
    # newly-populated residuals.
    _ml_bias_cache["map"] = None
    _ml_bias_cache["computed_at"] = 0.0
    _state_cache.clear()
    return {
        "replayed": replayed,
        "skipped": skipped,
        "failed": failed,
        "total_replay_rows_now": db.count_ml_replay_rows(),
    }


@app.post("/api/ml/runs/dedupe")
def ml_runs_dedupe():
    """Bulk-archive duplicate ml_runs rows. Keeps the most recent row
    for each unique (algorithm, n_train, ROUND(test_mae, 4)) tuple as
    'champion' and demotes everything else to 'archived'. Idempotent —
    safe to call repeatedly; subsequent calls have no further effect."""
    c = db._conn_or_init()
    with db._lock:
        cur = c.execute(
            """
            UPDATE ml_runs SET role='archived'
            WHERE id NOT IN (
                SELECT MAX(id) FROM ml_runs
                WHERE COALESCE(skipped, 0) = 0
                GROUP BY algorithm, n_train, ROUND(COALESCE(test_mae, -1), 4)
            )
            AND role = 'champion'
            AND COALESCE(skipped, 0) = 0
            """
        )
        archived = cur.rowcount
        # Also count remaining champions per algorithm so the response
        # makes the shape of the cleanup obvious.
        survivors = c.execute(
            "SELECT algorithm, COUNT(*) AS n FROM ml_runs "
            "WHERE role='champion' GROUP BY algorithm"
        ).fetchall()
        c.commit()
    return {
        "archived": archived,
        "champions_remaining": {r["algorithm"]: r["n"] for r in survivors},
    }


# ── Read-only SQL console ───────────────────────────────────────────────────

class SqlQueryIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=10_000)


def _strip_sql_comments(q: str) -> str:
    """Drop -- line comments and /* ... */ block comments before validating."""
    out = []
    i = 0
    n = len(q)
    in_block = False
    while i < n:
        if in_block:
            if q[i:i+2] == "*/":
                in_block = False
                i += 2
            else:
                i += 1
            continue
        if q[i:i+2] == "--":
            j = q.find("\n", i)
            if j == -1:
                break
            i = j + 1
            continue
        if q[i:i+2] == "/*":
            in_block = True
            i += 2
            continue
        out.append(q[i])
        i += 1
    return "".join(out)


_SQL_FORBIDDEN_TOKENS = (
    "insert ", "update ", "delete ", "drop ", "alter ", "create ",
    "replace ", "attach ", "detach ", "vacuum", "pragma ",
)


@app.post("/api/admin/sql")
def admin_sql(payload: SqlQueryIn):
    """Execute a read-only SELECT against the bets database. Returns
    {columns, rows, row_count, truncated, query_ms, query}.

    Hard rules: must start with SELECT or WITH, single statement only,
    no destructive verbs anywhere in the body, and a fixed row cap so a
    runaway cross-join doesn't OOM the server."""
    if SQL_CONSOLE_DISABLED:
        raise HTTPException(403, "sql console disabled (BETS_DISABLE_SQL_CONSOLE=1)")

    raw = payload.query.strip().rstrip(";").strip()
    if not raw:
        raise HTTPException(400, "empty query")

    cleaned = _strip_sql_comments(raw).strip()
    cleaned_lower = cleaned.lower()

    # Single-statement: no semicolons left after stripping comments + trailing
    # semicolons. (sqlite would only execute the first one anyway via execute,
    # but rejecting upfront keeps error messages honest.)
    if ";" in cleaned:
        raise HTTPException(400, "only a single statement is allowed")
    if not (cleaned_lower.startswith("select") or cleaned_lower.startswith("with")):
        raise HTTPException(400, "only SELECT (or WITH ... SELECT) queries are allowed")
    for tok in _SQL_FORBIDDEN_TOKENS:
        if tok in cleaned_lower:
            raise HTTPException(400, f"keyword not allowed in read-only console: {tok.strip()}")

    # Open a fresh read-only connection so even a loophole can't write.
    uri = f"file:{db.DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        t0 = time.time()
        try:
            cur = conn.execute(raw)
        except sqlite3.Error as exc:
            raise HTTPException(400, f"sql error: {exc}")
        cap = SQL_CONSOLE_ROW_CAP
        rows_raw = cur.fetchmany(cap + 1)
        truncated = len(rows_raw) > cap
        rows_raw = rows_raw[:cap]
        columns = [d[0] for d in (cur.description or [])]
        rows = [[r[c] for c in columns] for r in rows_raw]
        elapsed_ms = int((time.time() - t0) * 1000)
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "query_ms": elapsed_ms,
            "query": raw,
            "row_cap": cap,
        }
    finally:
        conn.close()


@app.get("/api/admin/sql/schema")
def admin_sql_schema():
    """List tables + their columns so the SQL console can show a quick
    schema reference without the user having to query sqlite_master."""
    if SQL_CONSOLE_DISABLED:
        raise HTTPException(403, "sql console disabled")
    uri = f"file:{db.DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        out = []
        for t in tables:
            cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            out.append({
                "name": t,
                "row_count": count,
                "columns": [{"name": c["name"], "type": c["type"], "notnull": bool(c["notnull"]), "pk": bool(c["pk"])} for c in cols],
            })
        return {"tables": out, "row_cap": SQL_CONSOLE_ROW_CAP}
    finally:
        conn.close()


@app.get("/api/accuracy")
def get_accuracy():
    """How close model_max came to the actual NWS high on each
    settled day. Joins feature_snapshots × bets.settled_max_f."""
    return db.accuracy_summary()


# ── ML pipeline endpoints ───────────────────────────────────────────────

@app.get("/api/ml/info")
def ml_info():
    """Latest training run + dataset counts + currently-loaded model + recent run history."""
    return {
        "data": db.historical_counts(),
        "latest_run": ml_train.latest_run_summary(),
        "recent_runs": db.list_ml_runs(limit=20),
        "predictor": ml_predict.info(),
        "retrain_interval_seconds": ML_RETRAIN_INTERVAL_SECONDS,
        "backfill_interval_seconds": ML_BACKFILL_INTERVAL_SECONDS,
        "retrain_loop_disabled": ML_RETRAIN_LOOP_DISABLED,
        "backfill_loop_disabled": ML_BACKFILL_LOOP_DISABLED,
        "backfill_progress": _ml_backfill_progress,
        "modelmax_source": _model_state["modelmax_source"],
        "blend_alpha": _model_state["blend_alpha"],
    }


@app.get("/api/ml/backtest")
def ml_backtest_endpoint(stake: float = 500.0, entry_cents: int = 25,
                          max_days: int = 365):
    """Replay the active model through historical paired data with a
    fixed-odds proxy (since we don't have historical Kalshi prices
    backfilled). Returns hit rate, total P/L, Sharpe, max drawdown,
    and a daily-P/L curve."""
    return ml_backtest.run_backtest(stake=stake, entry_cents=entry_cents,
                                     max_days=max_days)


@app.get("/api/ml/calibration")
def ml_calibration_endpoint():
    """Reliability diagram: predicted-probability vs actual-frequency
    per bucket. A perfectly calibrated model lies on the y=x line."""
    rows = ml_diagnostics.calibration_curve()
    return {"buckets": rows or []}


@app.get("/api/ml/adversarial")
def ml_adversarial_endpoint():
    """Adversarial validation — trains a binary classifier to tell
    'old' data from 'recent' data. AUC > 0.7 means distributions have
    diverged and the model is becoming stale."""
    return ml_diagnostics.adversarial_validation() or {"available": False}


@app.get("/api/ml/explain/{city_code}")
def ml_explain_endpoint(city_code: str):
    """Top-k SHAP contributions for the active prediction at this city.
    Returns null when shap isn't installed or the model type isn't
    introspectable. The Opportunities city drill-down can show this
    inline as 'why we predict X for NYC tomorrow'."""
    city = next((c for c in CITIES if c["code"] == city_code.upper()), None)
    if not city:
        return {"available": False, "error": "unknown city"}
    # Build a fresh feature dict the same way _build_city_state does. We
    # don't have the live numbers in this isolated handler, so we use
    # only the historical / aggregate features that are city-only.
    target_iso = _city_target_date(city)
    features = {
        "prev_actual_max_f": db.get_prev_actual(city["code"], target_iso) if target_iso else None,
        "lag3_actual_max_f": db.get_lag_actual(city["code"], target_iso, 3) if target_iso else None,
        "lag7_actual_max_f": db.get_lag_actual(city["code"], target_iso, 7) if target_iso else None,
        "roll7_mean_max_f": db.get_rolling_mean_actual(city["code"], target_iso, days=7) if target_iso else None,
        "seasonal_avg_max_f": db.get_seasonal_avg(city["code"], target_iso) if target_iso else None,
    }
    rows = ml_explain.explain_one(features, city["code"], top_k=8)
    return {
        "city": city["code"],
        "available": rows is not None,
        "top_features": rows or [],
    }


@app.get("/api/ml/online/info")
def ml_online_info():
    """Status of the incremental / online model (n_updates, last update ts,
    whether River is installed)."""
    return ml_online.info()


@app.get("/api/ml/events")
def ml_events(limit: int = 200, since_ts: Optional[int] = None,
              kinds: Optional[str] = None):
    """Activity feed for the ML pipeline. Each event is one entry the
    UI can render: backfill_start, retrain_done, model_swap, drift_alert,
    etc. `kinds` is comma-separated for client-side filtering."""
    kinds_list = [k.strip() for k in kinds.split(",")] if kinds else None
    events = db.list_ml_events(limit=limit, since_ts=since_ts, kinds=kinds_list)

    # Live status: training/backfilling in flight if a *_start event is
    # newer than its matching *_done. Cheap to compute on top of the list.
    def in_flight(start_kind, done_kind):
        s = db.latest_event(start_kind)
        d = db.latest_event(done_kind)
        if not s:
            return False
        if not d:
            return True
        return s["ts"] > d["ts"]

    return {
        "events": events,
        "in_flight": {
            "backfill": in_flight("backfill_start", "backfill_done"),
            "retrain": in_flight("retrain_start", "retrain_done"),
        },
    }


@app.get("/api/ml/model-diff")
def ml_model_diff_endpoint():
    """Compare the two most-recent persisted models — what changed in
    accuracy + which features shifted in importance."""
    diff = ml_diagnostics.model_diff()
    return diff or {"available": False}


def _sanitize_floats(o):
    """Walk a JSON-serializable structure and replace NaN / Inf floats
    with None so FastAPI's default encoder doesn't 500. Comes up when a
    feature column has zero variance (corrcoef → 0/0 → NaN) — the math
    upstream still warns, but the response no longer breaks the UI."""
    import math as _math
    if isinstance(o, float):
        if _math.isnan(o) or _math.isinf(o):
            return None
        return o
    if isinstance(o, dict):
        return {k: _sanitize_floats(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sanitize_floats(x) for x in o]
    return o


@app.get("/api/ml/diagnostics")
def ml_diagnostics_endpoint():
    """Statistical diagnostics for the active ML model: feature
    importance, correlation with target, recent residuals, MAE by
    week, per-city accuracy. Powers the Diagnostics panel on the ML
    tab — same kind of view you'd build in R-studio after fitting."""
    return _sanitize_floats(ml_diagnostics.diagnostics_summary())


class ModelConfigIn(BaseModel):
    modelmax_source: Optional[str] = Field(None, pattern="^(auto|ml|blend|ensemble)$")
    blend_alpha: Optional[float] = Field(None, ge=0, le=1)


class BiasConfigIn(BaseModel):
    enabled: Optional[bool] = None
    half_life_days: Optional[float] = Field(None, gt=0, le=365)
    lookback_days: Optional[int] = Field(None, gt=0, le=3650)
    max_correction: Optional[float] = Field(None, ge=0, le=20)


@app.get("/api/bias")
def bias_inspect():
    """Per-(city, horizon-bucket) forecast bias. Positive mean_residual =
    raw model has been UNDER-forecasting that city → we ADD it back to
    correct. Computed with exponential decay (recent days weighted more)."""
    bmap = _get_bias_map() or {}
    rows = [
        {"city": city, "horizon": bucket, **stats}
        for (city, bucket), stats in bmap.items()
    ]
    rows.sort(key=lambda r: (r["city"], r["horizon"]))
    return {
        "rows": rows,
        "config": {
            "enabled":         _model_state.get("bias_correction", True),
            "half_life_days":  _model_state.get("bias_half_life_days"),
            "lookback_days":   _model_state.get("bias_lookback_days"),
            "max_correction":  _model_state.get("bias_max_correction"),
        },
        "cache_age_seconds": int(time.time() - (_bias_cache.get("computed_at") or 0)),
        "cache_ttl_seconds": _BIAS_CACHE_TTL_SECONDS,
    }


@app.post("/api/bias/refresh")
def bias_refresh():
    """Invalidate BOTH bias caches (ensemble + ML) and recompute. Next
    request that needs a forecast picks up the fresh residuals."""
    _bias_cache["map"] = None
    _bias_cache["computed_at"] = 0.0
    _ml_bias_cache["map"] = None
    _ml_bias_cache["computed_at"] = 0.0
    bmap = _get_bias_map() or {}
    ml_bmap = _get_ml_bias_map() or {}
    _state_cache.clear()  # regenerate state with new corrections too
    return {
        "refreshed": True,
        "ensemble_rows": len(bmap),
        "ml_rows": len(ml_bmap),
    }


@app.post("/api/bias/config")
def bias_config(c: BiasConfigIn):
    """Toggle bias correction on/off and tune the decay/lookback/cap."""
    if c.enabled is not None:
        _model_state["bias_correction"] = c.enabled
    if c.half_life_days is not None:
        _model_state["bias_half_life_days"] = c.half_life_days
        _bias_cache["map"] = None
    if c.lookback_days is not None:
        _model_state["bias_lookback_days"] = c.lookback_days
        _bias_cache["map"] = None
    if c.max_correction is not None:
        _model_state["bias_max_correction"] = c.max_correction
    _state_cache.clear()
    return {
        "enabled":         _model_state.get("bias_correction"),
        "half_life_days":  _model_state.get("bias_half_life_days"),
        "lookback_days":   _model_state.get("bias_lookback_days"),
        "max_correction":  _model_state.get("bias_max_correction"),
    }


@app.post("/api/model/config")
def model_config(c: ModelConfigIn):
    """Switch the active prediction source at runtime — no restart needed.
    Each new state build picks up the new value on the next call."""
    if c.modelmax_source is not None:
        _model_state["modelmax_source"] = c.modelmax_source
    if c.blend_alpha is not None:
        _model_state["blend_alpha"] = c.blend_alpha
    # Invalidate the state cache so the change is visible immediately
    # rather than waiting up to 10 minutes for the cache TTL.
    _state_cache.clear()
    return {**_model_state}


@app.post("/api/ml/backfill")
async def ml_backfill_endpoint(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    years: Optional[int] = None,
    deep: bool = False,
):
    """Kick off a historical backfill in the background. Returns
    immediately; poll /api/ml/info to watch progress.

    Window selection (in priority order):
      - explicit start_date / end_date (ISO YYYY-MM-DD)
      - deep=true       → as far back as Open-Meteo will give (~25 years)
      - years=N         → start = today - N years
      - neither         → defaults to 5 years

    Open-Meteo's historical-forecast API typically holds model archives
    back to ~2022. Older periods produce only IEM ASOS actuals (no
    paired predictions); the trainer ignores rows missing ensemble_max
    so this doesn't poison the dataset. deep=true backfills 2001 →
    yesterday so that any year Open-Meteo serves for any city gets
    captured.
    """
    if _ml_backfill_progress.get("running"):
        raise HTTPException(409, "backfill already running")
    today = datetime.now(timezone.utc).date()
    if not start_date:
        if deep:
            lookback_years = 25
        elif years is not None:
            lookback_years = years
        else:
            lookback_years = 5
        start_date = (today - timedelta(days=int(lookback_years * 365.25))).isoformat()
    if not end_date:
        end_date = (today - timedelta(days=1)).isoformat()
    _ml_backfill_progress.clear()
    _ml_backfill_progress.update({"running": True, "stage": "queued"})

    async def _runner():
        try:
            await ml_backfill.run_backfill(start_date, end_date, _ml_backfill_progress)
        except Exception as exc:  # noqa: BLE001
            _ml_backfill_progress["running"] = False
            _ml_backfill_progress["stage"] = "error"
            _ml_backfill_progress.setdefault("errors", []).append(
                f"{type(exc).__name__}: {exc}"
            )

    asyncio.create_task(_runner())
    return {"started": True, "start_date": start_date, "end_date": end_date}


@app.post("/api/ml/train")
def ml_train_endpoint(algorithm: str = "linear"):
    """Train a model right now on the current historical_predictions ×
    historical_actuals data. Returns the ml_runs row (or an error
    if there isn't enough data yet)."""
    return ml_train.train(algorithm=algorithm)


@app.post("/api/ml/cleanup")
def ml_cleanup_endpoint():
    """One-shot fix: delete historical_predictions rows where
    ensemble_max is NULL. These are orphans left by an earlier upsert
    bug where MOS-only inserts overwrote OM data. After running this,
    re-fire Backfill to repopulate the OM columns cleanly."""
    deleted = db.cleanup_orphan_predictions()
    return {"deleted_rows": deleted, "remaining": db.historical_counts()}


class PlaceOrderIn(BaseModel):
    ticker: str = Field(min_length=1)
    side: str = Field(pattern="^(yes|no)$")
    count: int = Field(gt=0)
    yes_price: Optional[int] = Field(None, ge=1, le=99)
    no_price: Optional[int] = Field(None, ge=1, le=99)
    action: str = Field("buy", pattern="^(buy|sell)$")
    type: str = Field("limit", pattern="^(limit|market)$")
    confirm: bool = False


@app.get("/api/kalshi/balance")
async def kalshi_balance():
    """Account balance — sanity check before placing real orders."""
    if not kalshi.configured():
        raise HTTPException(503, "Kalshi not configured")
    async with httpx.AsyncClient() as client:
        bal = await kalshi.fetch_balance(client)
    if bal is None:
        raise HTTPException(502, kalshi._client.last_error or "balance fetch failed")
    return bal


@app.post("/api/kalshi/order")
async def kalshi_order(o: PlaceOrderIn):
    """Submit a REAL Kalshi order. Real money. Safety guards:
      - confirm:true must be set explicitly
      - server-side cost cap via env BETS_MAX_ORDER_USD (default $25)
      - side must match the price field (yes_price for yes, etc.)
    Cost cap is the worst-case dollar exposure of the order."""
    if not kalshi.configured():
        raise HTTPException(503, "Kalshi not configured (see backend/.env.example)")
    if not o.confirm:
        raise HTTPException(400, "must pass confirm:true to place a real order")
    price = o.yes_price if o.side == "yes" else o.no_price
    if o.type == "limit" and price is None:
        raise HTTPException(400, f"limit order on side={o.side} requires {o.side}_price")
    if price is not None:
        cost_usd = (price * o.count) / 100.0
    else:
        cost_usd = o.count  # market order worst case = $1/contract
    cap_usd = float(os.getenv("BETS_MAX_ORDER_USD", "25"))
    if cost_usd > cap_usd:
        raise HTTPException(
            400,
            f"order cost ${cost_usd:.2f} exceeds BETS_MAX_ORDER_USD=${cap_usd:.2f}",
        )
    async with httpx.AsyncClient() as client:
        result = await kalshi.place_order(
            client,
            ticker=o.ticker, side=o.side, count=o.count,
            yes_price=o.yes_price, no_price=o.no_price,
            action=o.action, order_type=o.type,
        )
    if result is None:
        raise HTTPException(502, kalshi._client.last_error or "order placement failed")
    return result


@app.get("/api/kalshi/info")
def kalshi_info():
    """Non-secret view of current Kalshi auth state. Useful as a sanity
    check after editing backend/.env — reports whether creds are present
    and whether we currently hold a token."""
    return kalshi.info()


@app.get("/api/kalshi/test")
async def kalshi_test():
    """Force a fresh login against Kalshi and report the result. Use
    this to verify your creds work without restarting the server."""
    async with httpx.AsyncClient() as client:
        return await kalshi.login_test(client)


@app.get("/api/kalshi/markets/{event_ticker}")
async def kalshi_event_markets(event_ticker: str):
    """List markets under a Kalshi event ticker. Useful for discovering
    the bracket-market schema before wiring real prices into the ladder."""
    if not kalshi.configured():
        raise HTTPException(503, "Kalshi not configured (see backend/.env.example)")
    async with httpx.AsyncClient() as client:
        markets = await kalshi.fetch_markets(client, event_ticker)
    if markets is None:
        raise HTTPException(502, "Kalshi fetch failed")
    return {"event_ticker": event_ticker, "count": len(markets), "markets": markets}


@app.get("/api/kalshi/events")
async def kalshi_events(series_ticker: Optional[str] = None, status: str = "open", limit: int = 50):
    """List Kalshi events. Use ?series_ticker=KXHIGHNY (or whatever the
    real weather series ticker turns out to be) to find tomorrow's
    daily-high event for a city."""
    if not kalshi.configured():
        raise HTTPException(503, "Kalshi not configured")
    async with httpx.AsyncClient() as client:
        events = await kalshi._client.fetch_events(client, series_ticker=series_ticker, status=status, limit=limit)
    if events is None:
        raise HTTPException(502, "Kalshi fetch failed")
    return {"count": len(events), "events": events}


@app.get("/api/debug/sounding/{code}")
async def sounding_debug(code: str):
    """Inspect why fetch_sounding returns None — surfaces URL, HTTP
    status, body length, and parsed levels."""
    city = next((c for c in CITIES if c["code"] == code.upper()), None)
    if not city:
        raise HTTPException(404, f"unknown city {code}")
    async with httpx.AsyncClient() as client:
        return await fetch_sounding_raw(client, city["sounding"])


@app.get("/api/debug/nws/{code}")
async def nws_debug(code: str):
    """Inspect raw NWS forecast periods for a city — find out which one
    fetch_nws_forecast_max is picking and why."""
    city = next((c for c in CITIES if c["code"] == code.upper()), None)
    if not city:
        raise HTTPException(404, f"unknown city {code}")
    target = _city_target_date(city)
    async with httpx.AsyncClient() as client:
        periods = await fetch_nws_forecast_periods(client, city["lat"], city["lon"])
    if periods is None:
        raise HTTPException(502, "NWS fetch failed")
    summary = [
        {
            "name": p.get("name"),
            "start": p.get("startTime", "")[:16],
            "isDaytime": p.get("isDaytime"),
            "temperature": p.get("temperature"),
            "match": p.get("startTime", "")[:10] == target and p.get("isDaytime"),
        }
        for p in periods[:8]
    ]
    return {"city": code.upper(), "target_date": target, "periods": summary}


@app.get("/api/kalshi/debug/{code}")
async def kalshi_debug(code: str):
    """Bypass the state cache: directly probe what fetch_brackets_for_city
    returns for one city. Surfaces any silent failures."""
    city = next((c for c in CITIES if c["code"] == code.upper()), None)
    if not city:
        raise HTTPException(404, f"unknown city {code}")
    if not kalshi.configured():
        raise HTTPException(503, "Kalshi not configured")
    series = city.get("kalshi_series")
    async with httpx.AsyncClient() as client:
        event = await kalshi._client.fetch_active_event(client, series)
        markets = (
            await kalshi._client.fetch_event_markets(client, event["event_ticker"])
            if event else None
        )
        brackets = await kalshi.fetch_brackets_for_city(client, series)
    return {
        "city": city["code"],
        "series": series,
        "active_event": event["event_ticker"] if event else None,
        "raw_market_count": len(markets) if markets is not None else None,
        "parsed_bracket_count": len(brackets) if brackets is not None else None,
        "brackets": brackets,
        "last_error": kalshi._client.last_error,
    }


@app.get("/api/kalshi/series")
async def kalshi_series(category: Optional[str] = None, limit: int = 100):
    """List Kalshi series. Pass ?category=Weather to narrow to weather
    markets and find the right series tickers for each city."""
    if not kalshi.configured():
        raise HTTPException(503, "Kalshi not configured")
    async with httpx.AsyncClient() as client:
        series = await kalshi._client.fetch_series(client, category=category, limit=limit)
    if series is None:
        raise HTTPException(502, "Kalshi fetch failed")
    return {"count": len(series), "series": series}


@app.get("/api/equity")
def get_equity(hours: float = 72.0, starting_balance: Optional[float] = None):
    """Live session equity curve. Per-tick equity = baseline + sum of
    open-position P/L at that snapshot tick. The baseline defaults to the
    sum of all account deposits (not the current balance — we want the
    curve anchored to "money put in" so withdrawals don't depress it).
    Empty until at least one bet has been placed and one snapshot loop
    has run."""
    if starting_balance is None:
        # Total ever deposited; if nothing has been deposited yet, fall back
        # to the current balance so the curve still has a sensible baseline.
        c = db._conn_or_init()
        row = c.execute(
            "SELECT COALESCE(SUM(amount), 0.0) AS d FROM account_transactions WHERE type = 'deposit'"
        ).fetchone()
        starting_balance = float(row["d"] or 0.0) or db.account_balance() or 0.0
    return db.list_equity_points(hours=hours, starting_balance=float(starting_balance))


@app.get("/api/snapshots/{code}")
def get_snapshots(code: str, hours: float = 24.0, limit: int = 500):
    """Edge / model-max history for one city. `hours` controls the window;
    default 24h. Returns rows ordered oldest → newest for direct sparkline
    consumption."""
    rows = db.list_snapshots(code, hours=hours, limit=limit)
    return [
        {
            "ts": r["ts"],
            "modelMax": r["model_max"],
            "bestEdgeCents": r["best_edge_cents"],
            "bestBracket": r["best_bracket_label"],
            "kalshiPct": r["best_kalshi_pct"],
        }
        for r in rows
    ]


def _settled_bet_to_position(b: Dict) -> Dict:
    """Project a settled bet row into the same shape /api/positions returns
    so the P&L "Closed positions" panel can render it without branching.
    actualMaxF mirrors settled_max_f so the bet card's State-3 (Final)
    block can render without needing a separate historical_actuals lookup."""
    entry = b["entry_cents"] / 100
    size = b["size"]
    settled_pl = b.get("settled_pl") or 0
    win = settled_pl > 0
    settled_max = b.get("settled_max_f")
    return {
        "id": f"P{b['id']}",
        "city": b["city"],
        "bracket": b["bracket_label"],
        "bracketLo": b.get("bracket_lo"),
        "bracketHi": b.get("bracket_hi"),
        "side": b["side"],
        "size": size,
        "entry": round(entry, 3),
        "current": 1.0 if win else 0.0,
        "ifWin": settled_pl if win else round((1 - entry) * (size / entry), 2) if b["side"] == "YES" else round(entry * (size / (1 - entry)), 2),
        "ifLose": settled_pl if not win else -size,
        "liveStatus": "locked_win" if win else "locked_loss",
        "maxSoFarF": settled_max,
        "actualMaxF": settled_max,
        "degreesFromBracket": 0.0,
        "targetDate": b.get("target_date"),
        "marketClosed": True,
        "settled": True,
        "settledAt": b.get("settled_at"),
        "settledPl": settled_pl,
        "placedAt": b.get("placed_at"),
    }


@app.get("/api/closed-positions")
def get_closed_positions():
    """All-time settled bets, projected into position shape so the P&L
    'Closed positions' panel can render the full history (not just bets that
    happen to still be open today)."""
    return [_settled_bet_to_position(b) for b in db.list_settled_bets()]


@app.get("/api/positions")
async def get_positions():
    """Open positions marked to market against the latest cached state."""
    open_bets = db.list_open_bets()
    if not open_bets:
        return []
    async with httpx.AsyncClient() as client:
        states = await asyncio.gather(
            *[_build_state_cached(c, client) for c in CITIES]
        )
    state_by_city = {s["city"]["code"]: s for s in states if s}
    out = []
    for b in open_bets:
        s = state_by_city.get(b["city"])
        if s is None:
            # No live data for this city — mark at entry.
            out.append(_mark_bet_to_market(b, b["entry_cents"] / 100))
            continue
        bracket = next(
            (br for br in s["brackets"] if br["label"] == b["bracket_label"]),
            None,
        )
        current_yes_pct = bracket["kalshiPct"] if bracket else b["entry_cents"] / 100
        out.append(_mark_bet_to_market(b, current_yes_pct, city_state=s))
    return out


@app.get("/api/state")
async def get_state():
    async with httpx.AsyncClient() as client:
        states = await asyncio.gather(*[_build_state_cached(c, client) for c in CITIES])
    return [s for s in states if s is not None]


@app.get("/api/state/{code}")
async def get_city_state(code: str, refresh: bool = False):
    city = next((c for c in CITIES if c["code"] == code.upper()), None)
    if not city:
        return {"error": f"unknown city {code}"}
    if refresh:
        _state_cache.pop(city["code"], None)
    async with httpx.AsyncClient() as client:
        state = await _build_state_cached(city, client)
    return state or {"error": "no upstream data"}


# ── Static file serving ─────────────────────────────────────────────────────
# Serve the prototype (Bets.html, styles.css, *.jsx) directly from the same
# FastAPI process so a single uvicorn invocation runs the whole stack.
# Mounted last so /api/* routes above take priority.
REPO_ROOT = Path(__file__).parent.parent


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/Bets.html")


app.mount("/", StaticFiles(directory=str(REPO_ROOT), html=False), name="static")
