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
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import db
from .cities import CITIES

SNAPSHOT_INTERVAL_SECONDS = int(os.getenv("BETS_SNAPSHOT_INTERVAL", "300"))  # 5 min default
SNAPSHOT_LOOP_DISABLED = os.getenv("BETS_DISABLE_SNAPSHOT_LOOP") == "1"
_snapshot_task: Optional[asyncio.Task] = None
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
    om, ecmwf_c, gfs_mos_f, nam_mos_f, afd_text, nws_max_f, sounding = await asyncio.gather(
        fetch_open_meteo(client, city["lat"], city["lon"]),
        fetch_ecmwf_max(client, city["lat"], city["lon"]),
        fetch_iem_mos(client, city["station"], "GFS"),
        fetch_iem_mos(client, city["station"], "NAM"),
        fetch_afd(client, city["office"]),
        fetch_nws_forecast_max(client, city["lat"], city["lon"]),
        fetch_sounding(client, city["sounding"]),
    )

    if not om:
        return None

    daily = om.get("daily", {})
    hourly = om.get("hourly", {})
    times = hourly.get("time", [])
    if len(daily.get("temperature_2m_max") or []) < 2 or not times:
        return None

    om_max_f = c_to_f(daily["temperature_2m_max"][1])
    obs_now_f = c_to_f(hourly["temperature_2m"][0])
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


async def _snapshot_loop() -> None:
    """Persist a snapshot of every city's state on a fixed cadence so the
    frontend can render real edge-history sparklines. Snapshots are skipped
    when upstream is unreachable (state is None) so we never poison the
    time series with synthetic fallback data."""
    while True:
        try:
            async with httpx.AsyncClient() as client:
                states = await asyncio.gather(
                    *[_build_state_cached(c, client) for c in CITIES]
                )
            for s in states:
                if s is not None:
                    db.insert_snapshot(s)
        except Exception as exc:  # noqa: BLE001 — best-effort background task
            print(f"[bets] snapshot loop error: {exc}")
        await asyncio.sleep(SNAPSHOT_INTERVAL_SECONDS)


@app.on_event("startup")
async def _startup() -> None:
    db.init()
    global _snapshot_task
    if not SNAPSHOT_LOOP_DISABLED and _snapshot_task is None:
        _snapshot_task = asyncio.create_task(_snapshot_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _snapshot_task
    if _snapshot_task is not None:
        _snapshot_task.cancel()
        try:
            await _snapshot_task
        except (asyncio.CancelledError, Exception):
            pass
        _snapshot_task = None


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
    row = db.insert_bet(
        city=b.city.upper(),
        bracket_label=b.bracket_label,
        bracket_lo=b.bracket_lo,
        bracket_hi=b.bracket_hi,
        side=b.side,
        size=b.size,
        entry_cents=b.entry_cents,
    )
    return _bet_row_to_log(row)


@app.get("/api/bets")
def get_bets(limit: int = 200):
    return [_bet_row_to_log(b) for b in db.list_bets(limit)]


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
