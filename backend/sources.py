import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import httpx

USER_AGENT = "kalshi-weather-bets/0.1 (research; contact: dylan@example.com)"

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
IEM_MOS_URL = "https://mesonet.agron.iastate.edu/mos/csv.php"
IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
NWS_API = "https://api.weather.gov"
# U. Wyoming retired their cgi-bin/sounding.py around 2024-25. The
# modern path is wsgi/sounding (no .py extension). We try the new URL
# first and fall back to the legacy one in case it ever comes back.
UWYO_SOUNDING_URLS = [
    "https://weather.uwyo.edu/wsgi/sounding",
    "https://weather.uwyo.edu/cgi-bin/sounding.py",
]
UWYO_SOUNDING_URL = UWYO_SOUNDING_URLS[0]  # backwards-compat for tests
AVWX_METAR_URL = "https://aviationweather.gov/api/data/metar"


async def fetch_open_meteo(client: httpx.AsyncClient, lat: float, lon: float) -> Optional[dict]:
    """Surface forecast + 850/700/500mb pressure levels for next 2 days. Temps in C, winds in kt."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join([
            "temperature_2m",
            "temperature_850hPa",
            "temperature_700hPa",
            "temperature_500hPa",
            "geopotential_height_500hPa",
            "relative_humidity_850hPa",
            "wind_speed_850hPa",
            "wind_direction_850hPa",
        ]),
        "daily": "temperature_2m_max,temperature_2m_min",
        "models": "gfs_seamless",
        "wind_speed_unit": "kn",
        "forecast_days": 2,
        "timezone": "auto",
    }
    try:
        r = await client.get(OPEN_METEO_URL, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


async def fetch_ecmwf_max(client: httpx.AsyncClient, lat: float, lon: float) -> Optional[float]:
    """ECMWF IFS daily max temp for tomorrow, in Celsius. Open-Meteo
    renamed `ecmwf_ifs04` (0.4°) to `ecmwf_ifs025` (0.25°) — try the
    current name first, fall back to the legacy name."""
    for model_id in ("ecmwf_ifs025", "ecmwf_ifs04"):
        params = {
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "models": model_id,
            "forecast_days": 2,
            "timezone": "auto",
        }
        try:
            r = await client.get(OPEN_METEO_URL, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            highs = data.get("daily", {}).get("temperature_2m_max")
            if highs and len(highs) >= 2 and highs[1] is not None:
                return highs[1]
        except Exception:
            continue
    return None


def _parse_mos_csv(text: str, target_date: datetime) -> Optional[float]:
    """IEM MOS CSV. Real header from /mos/csv.php is:
        station,model,runtime,ftime,n_x,tmp,dpt,...
    where `n_x` holds the daily MAX (at 00Z rows, covering the previous
    12Z→00Z UTC window — daytime in ET/CT/PT) and MIN (at 12Z rows).

    For "max temperature on target_date in the city's local timezone",
    the relevant n_x lives on the row whose ftime is at 00Z UTC on
    target_date+1 (since the 12Z→00Z window of that day spans daytime
    of target_date in any US timezone).

    Falls back to scanning the hourly `tmp` column inside that window
    when n_x isn't populated (older runs / partial bulletins)."""
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if len(lines) < 2:
        return None
    header = [h.strip().lower() for h in lines[0].split(",")]
    try:
        ftime_col = header.index("ftime")
    except ValueError:
        return None
    nx_col = header.index("n_x") if "n_x" in header else None
    tmp_col = header.index("tmp") if "tmp" in header else None
    if nx_col is None and tmp_col is None:
        return None

    from datetime import timedelta
    target_nx_date = (target_date + timedelta(days=1)).date()
    window_start = datetime(target_date.year, target_date.month, target_date.day, 12, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(hours=12)

    nx_value: Optional[float] = None
    tmp_max: Optional[float] = None
    for line in lines[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) <= ftime_col:
            continue
        ftime_raw = parts[ftime_col].split("+")[0].strip()
        try:
            dt = datetime.strptime(ftime_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        # Primary: n_x daily max for our target day
        if (
            nx_value is None
            and nx_col is not None
            and dt.date() == target_nx_date
            and dt.hour == 0
            and len(parts) > nx_col
            and parts[nx_col]
        ):
            try:
                nx_value = float(parts[nx_col])
            except ValueError:
                pass

        # Fallback: max over hourly tmp inside the daytime window
        if (
            tmp_col is not None
            and len(parts) > tmp_col
            and parts[tmp_col]
            and window_start <= dt <= window_end
        ):
            try:
                t = float(parts[tmp_col])
                if tmp_max is None or t > tmp_max:
                    tmp_max = t
            except ValueError:
                pass
    return nx_value if nx_value is not None else tmp_max


async def fetch_iem_mos(client: httpx.AsyncClient, station: str, model: str = "GFS") -> Optional[float]:
    """GFS-MOS or NAM-MOS predicted max temp for tomorrow at this station, in Fahrenheit."""
    from datetime import timedelta
    today = datetime.now(timezone.utc)
    runtime = today.strftime("%Y-%m-%d 00:00")
    target_date = today + timedelta(days=1)
    params = {"station": station, "runtime": runtime, "model": model}
    try:
        r = await client.get(IEM_MOS_URL, params=params, timeout=15)
        r.raise_for_status()
        return _parse_mos_csv(r.text, target_date)
    except Exception:
        return None


async def fetch_afd(client: httpx.AsyncClient, office: str) -> Optional[str]:
    """Latest NWS Area Forecast Discussion product text for this office."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/ld+json"}
    try:
        list_url = f"{NWS_API}/products/types/AFD/locations/{office}"
        r = await client.get(list_url, headers=headers, timeout=15)
        r.raise_for_status()
        graph = r.json().get("@graph") or []
        if not graph:
            return None
        latest_id = graph[0]["id"]
        r2 = await client.get(f"{NWS_API}/products/{latest_id}", headers=headers, timeout=15)
        r2.raise_for_status()
        return r2.json().get("productText")
    except Exception:
        return None


async def fetch_climate_report(client: httpx.AsyncClient, office: str) -> Optional[str]:
    """Latest NWS Daily Climate Report (CLI) text for an office. CLI is
    issued multiple times per day; the morning bulletin includes finalized
    YESTERDAY data which we use for bet settlement."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/ld+json"}
    try:
        list_url = f"{NWS_API}/products/types/CLI/locations/{office}"
        r = await client.get(list_url, headers=headers, timeout=15)
        r.raise_for_status()
        graph = r.json().get("@graph") or []
        if not graph:
            return None
        latest_id = graph[0]["id"]
        r2 = await client.get(f"{NWS_API}/products/{latest_id}", headers=headers, timeout=15)
        r2.raise_for_status()
        return r2.json().get("productText")
    except Exception:
        return None


async def fetch_nws_forecast_max(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    target_date_iso: Optional[str] = None,
) -> Optional[float]:
    """Official NWS forecast max temp, in Fahrenheit. Two-step lookup via
    /points → /forecast.

    NWS returns periods like ["This Afternoon", "Tonight", "Monday",
    "Monday Night", ...]. The previous "first daytime period wins" was
    picking up "This Afternoon" — today's already-cooked high — when the
    server runs during the day. With target_date_iso passed, we filter
    to the daytime period whose startTime falls on that date so we get
    tomorrow's forecast deliberately."""
    headers = {"User-Agent": USER_AGENT}
    try:
        r = await client.get(f"{NWS_API}/points/{lat:.4f},{lon:.4f}", headers=headers, timeout=15)
        r.raise_for_status()
        forecast_url = r.json()["properties"]["forecast"]
        r2 = await client.get(forecast_url, headers=headers, timeout=15)
        r2.raise_for_status()
        periods = r2.json()["properties"]["periods"]
        for p in periods:
            if not p.get("isDaytime"):
                continue
            if target_date_iso:
                start = p.get("startTime", "")
                # startTime is local-iso e.g. "2026-05-04T06:00:00-04:00".
                if start[:10] != target_date_iso:
                    continue
            temp = p.get("temperature")
            if temp is not None and p.get("temperatureUnit") == "F":
                return float(temp)
        return None
    except Exception:
        return None


async def fetch_nws_forecast_periods(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
) -> Optional[list]:
    """Raw NWS forecast periods. Used by /api/debug/nws/{city} to inspect
    why fetch_nws_forecast_max picked the period it did."""
    headers = {"User-Agent": USER_AGENT}
    try:
        r = await client.get(f"{NWS_API}/points/{lat:.4f},{lon:.4f}", headers=headers, timeout=15)
        r.raise_for_status()
        forecast_url = r.json()["properties"]["forecast"]
        r2 = await client.get(forecast_url, headers=headers, timeout=15)
        r2.raise_for_status()
        return r2.json()["properties"]["periods"]
    except Exception:
        return None


def _parse_uwyo_sounding(html: str) -> Optional[dict]:
    """Pull 850/700/500mb levels out of a U. Wyoming sounding HTML response.

    The page wraps fixed-width columns in a <PRE> block:
        PRES  HGHT  TEMP  DWPT  RELH  MIXR  DRCT  SKNT ...
        (mb)  (m)   (C)   (C)   (%)   (g/kg) (deg) (knot)
    Some lines have missing fields when sensors drop out — we skip those.
    """
    m = re.search(r"<PRE>(.*?)</PRE>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    body = m.group(1)
    targets = (850, 700, 500)
    levels: dict[int, dict] = {}
    for line in body.splitlines():
        parts = line.split()
        # Need at least PRES HGHT TEMP DWPT to be useful.
        if len(parts) < 4:
            continue
        try:
            pres = float(parts[0])
            temp_c = float(parts[2])
            dwpt_c = float(parts[3])
        except (ValueError, IndexError):
            continue
        wdir = wspd = None
        if len(parts) >= 8:
            try:
                wdir = float(parts[6])
                wspd = float(parts[7])
            except ValueError:
                pass
        for tgt in targets:
            if tgt not in levels and abs(pres - tgt) <= 5:
                levels[tgt] = {
                    "temp_c": temp_c,
                    "dwpt_c": dwpt_c,
                    "wdir": wdir,
                    "wspd_kt": wspd,
                }
    return levels or None


async def fetch_metar(client: httpx.AsyncClient, station: str, hours: int = 6) -> Optional[dict]:
    """Latest METAR observation(s) for an ASOS station from
    aviationweather.gov. Returns the freshest reading plus a short
    history for trend computation. Doc §5 step 8: hyper-local signal at
    the exact station Kalshi settles against."""
    params = {"ids": station, "format": "json", "hours": str(hours)}
    headers = {"User-Agent": USER_AGENT}
    try:
        r = await client.get(AVWX_METAR_URL, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        # aviationweather returns newest first; keep that ordering for `history`.
        latest = data[0]
        if latest.get("temp") is None:
            return None
        return {
            "temp_c": float(latest["temp"]),
            "dewp_c": float(latest["dewp"]) if latest.get("dewp") is not None else None,
            "wind_dir": latest.get("wdir"),
            "wind_kt": latest.get("wspd"),
            "obs_ts": latest.get("obsTime"),
            "raw": latest.get("rawOb"),
            "history": [
                {"ts": d.get("obsTime"), "temp_c": d.get("temp")}
                for d in data if d.get("temp") is not None
            ],
        }
    except Exception:
        return None


def _sounding_param_variants(station_id: str, target: datetime) -> List[Dict]:
    """Param sets to try against UWyo's various endpoints. The new wsgi
    endpoint expects `id` instead of `STNM`; date format may also have
    changed. Try the most-likely-modern set first."""
    yyyy = target.strftime("%Y")
    mm = target.strftime("%m")
    dd = target.strftime("%d")
    return [
        # Modern wsgi schema (from "'id' is not specified" error message)
        {"id": station_id, "type": "TEXT:LIST", "datetime": f"{yyyy}-{mm}-{dd} 12:00"},
        {"id": station_id, "type": "TEXT:LIST", "year": yyyy, "month": mm,
         "from": dd + "12", "to": dd + "12"},
        # Legacy uppercase-keys schema (worked on the old cgi-bin URL)
        {"STNM": station_id, "TYPE": "TEXT:LIST", "YEAR": yyyy, "MONTH": mm,
         "FROM": dd + "12", "TO": dd + "12"},
    ]


async def fetch_sounding_raw(client: httpx.AsyncClient, station_id: str) -> dict:
    """Probe every (URL, param-set) combination and report what each
    returned, so we can see which schema the current UWyo endpoint
    actually accepts."""
    now = datetime.now(timezone.utc)
    target = now if now.hour >= 13 else now - timedelta(days=1)
    attempts = []
    parsed_final = None
    for url in UWYO_SOUNDING_URLS:
        for params in _sounding_param_variants(station_id, target):
            try:
                r = await client.get(url, params=params, timeout=20)
                body = r.text
                parsed = _parse_uwyo_sounding(body)
                attempts.append({
                    "url": str(r.request.url),
                    "status": r.status_code,
                    "body_len": len(body),
                    "body_excerpt": body[:200],
                    "parsed_levels": list(parsed.keys()) if parsed else None,
                })
                if parsed and parsed_final is None:
                    parsed_final = parsed
            except Exception as exc:
                attempts.append({"url": url, "params": params,
                                 "error": f"{type(exc).__name__}: {exc}"})
    return {"parsed": parsed_final, "attempts": attempts}


async def fetch_sounding(client: httpx.AsyncClient, station_id: str) -> Optional[dict]:
    """Pull the most recent 12Z radiosonde sounding from U. Wyoming.

    12Z launches at 7 AM ET / 4 AM PT and are typically posted by 13Z.
    Per the v3.0 doc §2.4, the 12Z sounding is the highest-value reading —
    it captures the morning atmospheric state before afternoon heating.

    Tries the (URL, param-schema) cartesian product and returns the first
    combination that parses successfully — UWyo migrated from cgi-bin
    to wsgi mid-2024 and the param keys changed at the same time."""
    now = datetime.now(timezone.utc)
    target = now if now.hour >= 13 else now - timedelta(days=1)
    for url in UWYO_SOUNDING_URLS:
        for params in _sounding_param_variants(station_id, target):
            try:
                r = await client.get(url, params=params, timeout=20)
                if r.status_code != 200:
                    continue
                parsed = _parse_uwyo_sounding(r.text)
                if parsed:
                    return parsed
            except Exception:
                continue
    return None


# ── Historical backfill ──────────────────────────────────────────────────

async def fetch_open_meteo_historical(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    start_date: str,   # ISO YYYY-MM-DD
    end_date: str,
) -> Optional[Dict]:
    """Pull historical forecasts via Open-Meteo Historical Forecast API —
    "what GFS / ECMWF / ICON predicted for date X as of run Y".

    Returns daily max temps (in °F) per model + the 12Z 850/700/500mb
    pressure level temps (in °C, matching the live ingest). One call
    per (city, date range). Free, no auth.

    The endpoint stitches multiple models in a single response when
    requested via &models=. We grab GFS, ECMWF, ICON and surface +
    pressure-level fields in one round trip."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "daily": "temperature_2m_max",
        "hourly": ",".join([
            "temperature_2m",
            "temperature_850hPa",
            "temperature_700hPa",
            "temperature_500hPa",
            "geopotential_height_500hPa",
            "relative_humidity_850hPa",
        ]),
        "models": "gfs_seamless,ecmwf_ifs025,icon_seamless",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
    }
    try:
        r = await client.get(OPEN_METEO_HISTORICAL_FORECAST_URL, params=params, timeout=60)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _date_range(start_date: str, end_date: str) -> List[str]:
    s = datetime.fromisoformat(start_date).date()
    e = datetime.fromisoformat(end_date).date()
    out = []
    cur = s
    while cur <= e:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


async def fetch_iem_asos_daily_max(
    client: httpx.AsyncClient,
    station: str,    # e.g. "NYC" (without the K prefix)
    start_date: str,
    end_date: str,
) -> Optional[Dict[str, float]]:
    """Pull hourly ASOS observations from IEM and reduce to daily max
    Fahrenheit. Returns {target_date: max_f, ...}. Free, no auth, no
    rate limit. Used to backfill the actual NWS-equivalent daily highs.

    IEM ASOS station codes are the 3-letter ICAO suffix (NYC, ORD, MIA,
    AUS, LAX) — strip the leading K when calling. CSV contains hourly
    rows; we groupby date in city local time and take max."""
    station_short = station.lstrip("K")
    params = {
        "station": station_short,
        "data": "tmpf",
        "year1": start_date[:4], "month1": start_date[5:7], "day1": start_date[8:10],
        "year2": end_date[:4],   "month2": end_date[5:7],   "day2": end_date[8:10],
        "tz": "America/New_York",  # IEM accepts any IANA tz; we group by local date below
        "format": "onlycomma",
        "latlon": "no",
        "missing": "M",
        "trace": "T",
        "direct": "no",
        "report_type": "3",  # MADIS hourly
    }
    try:
        r = await client.get(IEM_ASOS_URL, params=params, timeout=120)
        r.raise_for_status()
        text = r.text
    except Exception:
        return None

    # Header looks like: station,valid,tmpf
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    if len(lines) < 2:
        return None
    header = [h.strip().lower() for h in lines[0].split(",")]
    try:
        valid_col = header.index("valid")
        tmpf_col = header.index("tmpf")
    except ValueError:
        return None

    by_date: Dict[str, float] = {}
    for line in lines[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) <= max(valid_col, tmpf_col):
            continue
        try:
            ts = datetime.strptime(parts[valid_col], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        date_key = ts.date().isoformat()
        try:
            tmpf = float(parts[tmpf_col])
        except ValueError:
            continue
        if date_key not in by_date or tmpf > by_date[date_key]:
            by_date[date_key] = tmpf
    return by_date or None
