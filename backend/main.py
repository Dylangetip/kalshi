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
    quotes if present. Existing process env wins (so explicit exports
    override the file)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(Path(__file__).parent / ".env")

from . import db, kalshi
from .cities import CITIES
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
_snapshot_task: Optional[asyncio.Task] = None
_settlement_task: Optional[asyncio.Task] = None
from .model import (
    best_bracket,
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
    fetch_open_meteo,
    fetch_sounding,
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
    om, ecmwf_c, gfs_mos_f, nam_mos_f, afd_text, nws_max_f, sounding, metar = await asyncio.gather(
        fetch_open_meteo(client, city["lat"], city["lon"]),
        fetch_ecmwf_max(client, city["lat"], city["lon"]),
        fetch_iem_mos(client, city["station"], "GFS"),
        fetch_iem_mos(client, city["station"], "NAM"),
        fetch_afd(client, city["office"]),
        fetch_nws_forecast_max(client, city["lat"], city["lon"]),
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

    afd = parse_afd(afd_text)
    # Tighter sigma when AFD says high confidence; wider when models disagree.
    sigma = 2.4 - (afd["score"] - 3) * 0.25
    # Doc §3.1: Kalshi center brackets are systematically overpriced — model with widening.
    ladder = compute_ladder(model_max, model_sigma=max(1.2, sigma), kalshi_widening=1.30)
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
        "settlementBracket": best,
        "modelMax": round(model_max, 1),
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


@app.on_event("startup")
async def _startup() -> None:
    db.init()
    global _snapshot_task, _settlement_task
    if not SNAPSHOT_LOOP_DISABLED and _snapshot_task is None:
        _snapshot_task = asyncio.create_task(_snapshot_loop())
    if not SETTLEMENT_LOOP_DISABLED and _settlement_task is None:
        _settlement_task = asyncio.create_task(_settlement_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _snapshot_task, _settlement_task
    for name in ("_snapshot_task", "_settlement_task"):
        task = globals().get(name)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            globals()[name] = None


class PlaceBetIn(BaseModel):
    city: str
    bracket_label: str
    bracket_lo: int
    bracket_hi: int
    side: str = Field(pattern="^(YES|NO)$")
    size: int = Field(gt=0)
    entry_cents: int = Field(ge=0, le=100)


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
async def get_city_state(code: str):
    city = next((c for c in CITIES if c["code"] == code.upper()), None)
    if not city:
        return {"error": f"unknown city {code}"}
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
