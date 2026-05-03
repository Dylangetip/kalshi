import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

USER_AGENT = "kalshi-weather-bets/0.1 (research; contact: dylan@example.com)"

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
IEM_MOS_URL = "https://mesonet.agron.iastate.edu/mos/csv.php"
NWS_API = "https://api.weather.gov"
UWYO_SOUNDING_URL = "https://weather.uwyo.edu/cgi-bin/sounding.py"
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


async def fetch_sounding(client: httpx.AsyncClient, station_id: str) -> Optional[dict]:
    """Pull the most recent 12Z radiosonde sounding from U. Wyoming.

    12Z launches at 7 AM ET / 4 AM PT and are typically posted by 13Z.
    Per the v3.0 doc §2.4, the 12Z sounding is the highest-value reading —
    it captures the morning atmospheric state before afternoon heating.
    """
    now = datetime.now(timezone.utc)
    # If we're past 13Z, today's 12Z is fresh; otherwise reach back to yesterday.
    target = now if now.hour >= 13 else now - timedelta(days=1)
    params = {
        "TYPE": "TEXT:LIST",
        "YEAR": target.strftime("%Y"),
        "MONTH": target.strftime("%m"),
        "FROM": target.strftime("%d") + "12",
        "TO": target.strftime("%d") + "12",
        "STNM": station_id,
    }
    try:
        r = await client.get(UWYO_SOUNDING_URL, params=params, timeout=20)
        r.raise_for_status()
        return _parse_uwyo_sounding(r.text)
    except Exception:
        return None
