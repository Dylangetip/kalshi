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
import os
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
AUTO_TRADE_INTERVAL_SECONDS = int(os.getenv("BETS_AUTO_TRADE_INTERVAL", "3600"))
_auto_trade_state: Dict = {
    "enabled": os.getenv("BETS_AUTO_TRADE") == "1",
    "min_edge_cents": int(os.getenv("BETS_AUTO_TRADE_MIN_EDGE", "3")),
    "bankroll": float(os.getenv("BETS_AUTO_TRADE_BANKROLL", "10000")),
    # Default max-per-bet = 5% of bankroll (¼-Kelly cap). Recomputed
    # automatically when bankroll changes via /api/auto-trade/config
    # unless the user explicitly sets max_usd in the same request.
    "max_usd": float(os.getenv("BETS_AUTO_TRADE_MAX_USD",
                               str(float(os.getenv("BETS_AUTO_TRADE_BANKROLL", "10000")) * 0.05))),
    "min_usd": float(os.getenv("BETS_AUTO_TRADE_MIN_USD", "50")),
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
ML_RETRAIN_LOOP_DISABLED = os.getenv("BETS_DISABLE_ML_RETRAIN_LOOP") == "1"

# Incremental-backfill loop: every ML_BACKFILL_INTERVAL_SECONDS, pull
# the last 7 days into historical_predictions / historical_actuals so
# the next retrain has fresh examples. Idempotent thanks to
# INSERT OR REPLACE in the upsert helpers.
ML_BACKFILL_INTERVAL_SECONDS = int(os.getenv("BETS_ML_BACKFILL_INTERVAL", "86400"))
ML_BACKFILL_LOOP_DISABLED = os.getenv("BETS_DISABLE_ML_BACKFILL_LOOP") == "1"

# Backfill progress shared dict (read by /api/ml/backfill/status).
_ml_backfill_progress: Dict = {"running": False, "stage": "idle"}
from .model import (
    best_bracket,
    bracket_prob,
    compute_ladder,
    ensemble_model_max,
    kelly_fraction,
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

    # Doc §2.1: MOS is the highest-value source; weight it accordingly.
    model_max = ensemble_model_max([
        (gfs_mos_f, 0.40),
        (nam_mos_f, 0.20),
        (nws_max_f, 0.15),
        (om_max_f, 0.15),
        (ecmwf_max_f, 0.10),
    ])
    if model_max is None:
        return None

    # Run the trained ML model in parallel for the comparison panel.
    # Inference uses only the features available historically (no MOS) so
    # the live + training feature schemas match. Returns None when no
    # model has been trained yet — callers handle gracefully.
    ml_max = ml_predict.predict_max(
        {
            "gfs_max": om_max_f,        # surface OM seamless ≈ GFS daily max
            "ecmwf_max": ecmwf_max_f,
            "icon_max": None,           # not currently in live state
            "om_max": om_max_f,
            "t850_c": t850_c,
            "t700_c": t700_c,
            "t500_c": t500_c,
            "h500_m": h500,
            "rh850_pct": rh850,
            "ensemble_max": model_max,
        },
        city=city["code"],
    )

    afd = parse_afd(afd_text)
    # Next-day temp forecasts have ~1.5-2°F std dev empirically, so default
    # σ = 1.8 with AFD adjustment. Tighter when AFD says high confidence,
    # wider when models disagree. Old σ=2.4 was calibrated for the
    # synthetic 2°F-wide ladder; the live Kalshi 1°F brackets need tighter.
    sigma = 1.8 - (afd["score"] - 3) * 0.20
    sigma = max(1.0, sigma)

    # Try real Kalshi prices first. When the API key is configured and the
    # bracket fetch succeeds, use the live market structure (Kalshi's own
    # bracket bounds + their YES prices) and compute model probabilities
    # over the same brackets via the same gauss centered on model_max.
    # Doc §3.1: Kalshi center brackets are systematically overpriced — the
    # synthetic ladder modeled this with a widening factor; with real
    # prices we just use them directly.
    kalshi_brackets = None
    if kalshi.configured() and city.get("kalshi_series"):
        kalshi_brackets = await kalshi.fetch_brackets_for_city(client, city["kalshi_series"])

    if kalshi_brackets:
        ladder = []
        for b in kalshi_brackets:
            model_pct = bracket_prob(
                b["lo"], b["hi"], model_max, sigma,
                lower_tail=b.get("lower_tail", False),
                upper_tail=b.get("upper_tail", False),
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
        ladder = compute_ladder(model_max, model_sigma=sigma, kalshi_widening=1.30)

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
        "mlMax": ml_max,
        "nwsForecast": round(nws_max_f, 1) if nws_max_f is not None else round(om_max_f, 1),
        "mosMax": round(gfs_mos_f, 1) if gfs_mos_f is not None else round(model_max, 1),
        "namMos": round(nam_mos_f, 1) if nam_mos_f is not None else round(model_max, 1),
        "obsCurrent": round(obs_now_f, 1) if obs_now_f is not None else round(model_max - 6, 1),
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
    while True:
        try:
            run = ml_train.train(algorithm="auto")
            if run.get("error"):
                print(f"[ml-retrain] auto skipped: {run['error']}")
            else:
                cands = run.get("auto_candidates") or []
                summary = ", ".join(f"{c['algorithm']}={c['test_mae']}" for c in cands)
                print(f"[ml-retrain] auto sweep: {summary} → kept {run.get('algorithm')}")
        except Exception as exc:  # noqa: BLE001
            print(f"[ml-retrain] error: {exc}")
        await asyncio.sleep(ML_RETRAIN_INTERVAL_SECONDS)


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
            async with httpx.AsyncClient() as client:
                await ml_backfill.run_backfill(start, end, _ml_backfill_progress)
            counts = db.historical_counts()
            print(f"[ml-backfill] tables now: {counts.get('paired')} paired rows")
        except Exception as exc:  # noqa: BLE001
            print(f"[ml-backfill] error: {exc}")
        # Sleep the configured interval BEFORE the next pull (the initial
        # 5-min wait above only runs once).
        await asyncio.sleep(max(1, ML_BACKFILL_INTERVAL_SECONDS - 300))


@app.on_event("startup")
async def _startup() -> None:
    db.init()
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


class AutoTradeConfigIn(BaseModel):
    enabled: Optional[bool] = None
    min_edge_cents: Optional[int] = Field(None, ge=0, le=100)
    bankroll: Optional[float] = Field(None, gt=0)
    max_usd: Optional[float] = Field(None, gt=0)
    min_usd: Optional[float] = Field(None, ge=0)


@app.get("/api/auto-trade/info")
def auto_trade_info():
    """Current runtime config + recent activity stats."""
    return {**_auto_trade_state, "interval_seconds": AUTO_TRADE_INTERVAL_SECONDS}


@app.post("/api/auto-trade/config")
def auto_trade_config(c: AutoTradeConfigIn):
    """Update auto-trader config at runtime — takes effect on the next
    loop tick (no restart needed). When bankroll is changed without an
    explicit max_usd in the same request, max_usd is auto-recomputed
    as 5% of bankroll (¼-Kelly cap)."""
    for k in ("enabled", "min_edge_cents", "bankroll", "max_usd", "min_usd"):
        v = getattr(c, k)
        if v is not None:
            _auto_trade_state[k] = v
    if c.bankroll is not None and c.max_usd is None:
        _auto_trade_state["max_usd"] = round(_auto_trade_state["bankroll"] * 0.05, 2)
    return {**_auto_trade_state, "interval_seconds": AUTO_TRADE_INTERVAL_SECONDS}


@app.post("/api/auto-trade/now")
async def auto_trade_now():
    """Manual one-shot trigger. Runs even when enabled=false — handy for
    testing config without flipping the loop on."""
    async with httpx.AsyncClient() as client:
        placed = await _auto_trade_tick(client)
    return {
        **_auto_trade_state,
        "interval_seconds": AUTO_TRADE_INTERVAL_SECONDS,
        "placed": placed,
        "count": len(placed),
    }


class PlaceBetIn(BaseModel):
    city: str
    bracket_label: str
    bracket_lo: int
    bracket_hi: int
    side: str = Field(pattern="^(YES|NO)$")
    size: int = Field(gt=0)
    entry_cents: int = Field(ge=0, le=100)


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


def _mark_bet_to_market(b: Dict, current_yes_pct: float) -> Dict:
    """Mark a stored bet against the latest Kalshi YES probability for its
    bracket. Matches the prototype's sign convention: entry/current are
    stored as YES probabilities, and P/L = (current - entry) * size * 100
    * (+1 for YES, -1 for NO) so a NO bet profits when YES drops."""
    entry = b["entry_cents"] / 100
    current = current_yes_pct
    sign = 1 if b["side"] == "YES" else -1
    pl = (current - entry) * b["size"] * sign * 100
    return {
        "id": f"P{b['id']}",
        "city": b["city"],
        "bracket": b["bracket_label"],
        "side": b["side"],
        "size": b["size"],
        "entry": round(entry, 3),
        "current": round(current, 3),
        "pl": round(pl, 2),
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
    row = db.insert_bet(
        city=city_code,
        bracket_label=b.bracket_label,
        bracket_lo=b.bracket_lo,
        bracket_hi=b.bracket_hi,
        side=b.side,
        size=b.size,
        entry_cents=b.entry_cents,
        target_date=target_date,
    )
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
            in_bracket = bet["bracket_lo"] <= actual_max <= bet["bracket_hi"]
            pl = settle_pl(bet["side"], bet["entry_cents"], bet["size"], in_bracket)
            db.settle_bet(bet["id"], pl, actual_max)
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


async def _auto_trade_tick(client: httpx.AsyncClient) -> List[Dict]:
    """One iteration of the auto-trader. Considers every bracket on every
    city's ladder (not just the top-recommended one) — so once we've bet
    the headline bracket, subsequent ticks can still find positive edge
    on neighboring brackets and keep firing. Picks the SINGLE highest-edge
    eligible bracket across all (city, bracket) pairs and places a
    ¼-Kelly YES bet on it. Idempotent across ticks AND restarts via
    list_bets_for_target."""
    cfg = _auto_trade_state
    candidates: List[tuple] = []  # (edge_cents, city, state, bracket, target)
    for city in CITIES:
        try:
            state = await _build_state_cached(city, client)
            if state is None:
                continue
            target = state.get("targetDate")
            if target is None:
                continue
            existing = db.list_bets_for_target(city["code"], target)
            existing_labels = {b["bracket_label"] for b in existing}
            for bracket in state.get("brackets") or []:
                edge_cents = int(round((bracket.get("edge") or 0) * 100))
                if edge_cents < cfg["min_edge_cents"]:
                    continue
                if bracket.get("label") in existing_labels:
                    continue
                candidates.append((edge_cents, city, state, bracket, target))
        except Exception as exc:  # noqa: BLE001
            print(f"[auto-trader] probe error on {city['code']}: {exc}")

    placed: List[Dict] = []
    if candidates:
        candidates.sort(key=lambda c: -c[0])
        edge_cents, city, state, bracket, target = candidates[0]
        # Recompute Kelly for the picked bracket since state.kellyPct only
        # reflects the recommended (max-edge) bracket.
        kelly = kelly_fraction(bracket.get("edge") or 0, bracket.get("kalshiPct") or 0.5)
        size = max(cfg["min_usd"], round(kelly * cfg["bankroll"]))
        size = int(min(cfg["max_usd"], size))
        if size >= cfg["min_usd"]:
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
                      f"(+{edge_cents}¢ edge, beat {len(candidates)-1} others) — "
                      f"size=${size} entry={row['entry_cents']}¢")
            except Exception as exc:  # noqa: BLE001
                print(f"[auto-trader] insert error: {exc}")

    cfg["last_run_ts"] = int(time.time())
    cfg["last_run_placed"] = len(placed)
    cfg["last_run_considered"] = len(candidates)
    cfg["total_runs"] += 1
    cfg["total_placed"] += len(placed)
    return placed


async def _auto_trader_loop() -> None:
    """Background auto-trader. Always runs (so UI toggles take effect
    without restart) — checks the enabled flag inside each iteration."""
    print(f"[auto-trader] loop active — interval={AUTO_TRADE_INTERVAL_SECONDS}s, "
          f"initial state={_auto_trade_state}")
    while True:
        try:
            if _auto_trade_state["enabled"]:
                async with httpx.AsyncClient() as client:
                    await _auto_trade_tick(client)
        except Exception as exc:  # noqa: BLE001
            print(f"[auto-trader] loop error: {exc}")
        await asyncio.sleep(AUTO_TRADE_INTERVAL_SECONDS)


@app.post("/api/settle")
async def settle_now():
    """Manual trigger for the settlement job. Useful for testing and for
    forcing a check after the morning CLI is known to have posted."""
    async with httpx.AsyncClient() as client:
        results = await _settle_open_bets(client)
    return {"settled": results, "count": len(results)}


@app.get("/api/stats")
def get_stats():
    return db.stats_summary()


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
    }


@app.post("/api/ml/backfill")
async def ml_backfill_endpoint(start_date: Optional[str] = None, end_date: Optional[str] = None):
    """Kick off a historical backfill in the background. Returns
    immediately; poll /api/ml/info to watch progress."""
    if _ml_backfill_progress.get("running"):
        raise HTTPException(409, "backfill already running")
    today = datetime.now(timezone.utc).date()
    if not start_date:
        start_date = (today - timedelta(days=3 * 365)).isoformat()
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
def get_equity(hours: float = 72.0, starting_balance: float = 10000.0):
    """Live session equity curve. Per-tick equity = starting bankroll + sum
    of open-position P/L at that snapshot tick. Empty until at least one
    bet has been placed and one snapshot loop has run."""
    return db.list_equity_points(hours=hours, starting_balance=starting_balance)


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
        out.append(_mark_bet_to_market(b, current_yes_pct))
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
