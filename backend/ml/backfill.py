"""Historical backfill of (predictions, actuals) for ML training.

For each city × date in the requested window:
  1. Pull the past forecasts that GFS / ECMWF / ICON were producing
     for that date via the Open-Meteo Historical Forecast API. One
     range query per city per ~90-day chunk.
  2. Compute the ensemble_max using the same weights the live ingest
     uses (so the ML model can learn how to correct the ensemble).
  3. Pull the actual NWS-station daily max from IEM ASOS hourly archive.

Insert OR REPLACE so the backfill is idempotent and incrementally
extensible — re-running with a wider window just fills the gaps.
"""

import asyncio
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

import httpx

from .. import db
from ..cities import CITIES
from ..sources import (
    fetch_iem_asos_daily_max,
    fetch_open_meteo_historical,
)


# Weights that the live ensemble uses on the subset of features we have
# historically. MOS/NWS aren't backfilled (live-only signals), so we
# renormalize across just the Open-Meteo models.
_HISTORICAL_ENSEMBLE_WEIGHTS = {
    "gfs_max":   0.50,
    "ecmwf_max": 0.30,
    "icon_max":  0.20,
}


def _ensemble_from_models(gfs: Optional[float], ecmwf: Optional[float], icon: Optional[float]) -> Optional[float]:
    weights = {"gfs_max": gfs, "ecmwf_max": ecmwf, "icon_max": icon}
    valid = [(_HISTORICAL_ENSEMBLE_WEIGHTS[k], v) for k, v in weights.items() if v is not None]
    if not valid:
        return None
    total_w = sum(w for w, _ in valid)
    return round(sum(w * v for w, v in valid) / total_w, 2)


def _hourly_at_target_local_afternoon(times: List[str], target: str) -> Optional[int]:
    """Index of the timestamp in `times` that corresponds to ~18:00 local
    on `target` date — a reasonable "afternoon snapshot" for upper-air
    fields. Falls back to noon, then midnight, then None."""
    for hour_str in ("T18:00", "T15:00", "T12:00", "T00:00"):
        for i, t in enumerate(times):
            if t.startswith(target) and t.endswith(hour_str):
                return i
    # Last resort: any hour on target date
    for i, t in enumerate(times):
        if t.startswith(target):
            return i
    return None


def _safe(arr, idx):
    if arr is None or idx is None or idx >= len(arr):
        return None
    val = arr[idx]
    return val


def _row_from_open_meteo(om_payload: Dict, target_date: str, forecast_horizon_hours: int) -> Optional[Dict]:
    """Extract the per-day prediction row from one Open-Meteo response.
    Open-Meteo's historical-forecast API returns multi-model time series
    keyed under `daily.<var>_<modelid>` when multiple models are
    requested via &models=. We pluck out each model's daily max and the
    upper-air state at afternoon-local on target_date."""
    daily = om_payload.get("daily") or {}
    times = daily.get("time") or []
    if target_date not in times:
        return None
    di = times.index(target_date)

    def _get_model_max(suffix):
        v = daily.get(f"temperature_2m_max_{suffix}")
        return v[di] if v and di < len(v) and v[di] is not None else None

    # When only one model is requested the var name has no suffix.
    gfs = _get_model_max("gfs_seamless") or (daily.get("temperature_2m_max", [None])[di] if di < len(daily.get("temperature_2m_max") or []) else None)
    ecmwf = _get_model_max("ecmwf_ifs025")
    icon = _get_model_max("icon_seamless")
    om_max = gfs  # treat gfs_seamless as the OM ensemble surface max

    hourly = om_payload.get("hourly") or {}
    h_times = hourly.get("time") or []
    afternoon_idx = _hourly_at_target_local_afternoon(h_times, target_date)
    t850 = _safe(hourly.get("temperature_850hPa"), afternoon_idx)
    t700 = _safe(hourly.get("temperature_700hPa"), afternoon_idx)
    t500 = _safe(hourly.get("temperature_500hPa"), afternoon_idx)
    h500 = _safe(hourly.get("geopotential_height_500hPa"), afternoon_idx)
    rh850 = _safe(hourly.get("relative_humidity_850hPa"), afternoon_idx)

    return {
        "target_date": target_date,
        "forecast_horizon_hours": forecast_horizon_hours,
        "gfs_max": gfs,
        "ecmwf_max": ecmwf,
        "icon_max": icon,
        "om_max": om_max,
        "t850_c": t850,
        "t700_c": t700,
        "t500_c": t500,
        "h500_m": int(h500) if h500 is not None else None,
        "rh850_pct": int(rh850) if rh850 is not None else None,
        "ensemble_max": _ensemble_from_models(gfs, ecmwf, icon),
    }


def _date_chunks(start: date, end: date, chunk_days: int = 90):
    """Yield (chunk_start, chunk_end) tuples covering [start, end] in
    ≤chunk_days windows. Open-Meteo's historical-forecast endpoint
    accepts multi-month ranges, so 90 days per request is fine."""
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=chunk_days - 1), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


async def run_backfill(
    start_date: str,
    end_date: str,
    progress: Optional[Dict] = None,
) -> Dict:
    """Pull (predictions, actuals) for [start_date, end_date] across all
    cities. Returns a summary dict. `progress` is an optional shared
    dict the caller can poll to render a progress bar."""
    if progress is None:
        progress = {}
    progress.update({
        "running": True,
        "stage": "starting",
        "start_date": start_date,
        "end_date": end_date,
        "cities_done": 0,
        "predictions_inserted": 0,
        "actuals_inserted": 0,
        "errors": [],
    })

    s = datetime.fromisoformat(start_date).date()
    e = datetime.fromisoformat(end_date).date()
    pred_count = 0
    act_count = 0

    async with httpx.AsyncClient() as client:
        for city in CITIES:
            try:
                progress["stage"] = f"backfilling {city['code']}"
                # 1. Predictions, in 90-day chunks
                for chunk_start, chunk_end in _date_chunks(s, e):
                    om = await fetch_open_meteo_historical(
                        client, city["lat"], city["lon"],
                        chunk_start.isoformat(), chunk_end.isoformat(),
                    )
                    if not om:
                        continue
                    cur = chunk_start
                    while cur <= chunk_end:
                        row = _row_from_open_meteo(om, cur.isoformat(), forecast_horizon_hours=24)
                        if row and row.get("ensemble_max") is not None:
                            row["city"] = city["code"]
                            db.upsert_historical_prediction(row)
                            pred_count += 1
                            progress["predictions_inserted"] = pred_count
                        cur += timedelta(days=1)

                # 2. Actuals from IEM ASOS hourly archive — single range pull
                asos = await fetch_iem_asos_daily_max(
                    client, city["station"],
                    start_date, end_date,
                )
                if asos:
                    for d, mx in asos.items():
                        db.upsert_historical_actual(city["code"], d, mx, "iem_asos")
                        act_count += 1
                        progress["actuals_inserted"] = act_count
            except Exception as exc:  # noqa: BLE001 — record and continue
                progress["errors"].append(f"{city['code']}: {type(exc).__name__}: {exc}")
            progress["cities_done"] += 1

    progress["running"] = False
    progress["stage"] = "done"
    counts = db.historical_counts()
    progress["counts"] = counts
    return progress
