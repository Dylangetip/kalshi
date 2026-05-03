import re
from datetime import datetime, timezone
from typing import Optional

import httpx

USER_AGENT = "kalshi-weather-bets/0.1 (research; contact: dylan@example.com)"

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
IEM_MOS_URL = "https://mesonet.agron.iastate.edu/mos/csv.php"
NWS_API = "https://api.weather.gov"


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
    """ECMWF IFS daily max temp for tomorrow, in Celsius."""
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max",
        "models": "ecmwf_ifs04",
        "forecast_days": 2,
        "timezone": "auto",
    }
    try:
        r = await client.get(OPEN_METEO_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data["daily"]["temperature_2m_max"][1]
    except Exception:
        return None


def _parse_mos_csv(text: str, target_date: datetime) -> Optional[float]:
    """IEM MOS bulletins come back as CSV with valid_time + tmpf columns. Return max F for target date."""
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if len(lines) < 2:
        return None
    header = [h.strip().lower() for h in lines[0].split(",")]
    try:
        time_col = next(i for i, h in enumerate(header) if "time" in h or "valid" in h)
        temp_col = next(i for i, h in enumerate(header) if h in ("tmp", "tmpf", "tmp_f"))
    except StopIteration:
        return None
    target = target_date.date()
    temps = []
    for line in lines[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) <= max(time_col, temp_col):
            continue
        try:
            dt = datetime.strptime(parts[time_col], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if dt.date() != target:
            continue
        try:
            temps.append(float(parts[temp_col]))
        except ValueError:
            continue
    return max(temps) if temps else None


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


async def fetch_nws_forecast_max(client: httpx.AsyncClient, lat: float, lon: float) -> Optional[float]:
    """Official NWS forecast max temp for tomorrow, in Fahrenheit. Two-step lookup via /points."""
    headers = {"User-Agent": USER_AGENT}
    try:
        r = await client.get(f"{NWS_API}/points/{lat:.4f},{lon:.4f}", headers=headers, timeout=15)
        r.raise_for_status()
        forecast_url = r.json()["properties"]["forecast"]
        r2 = await client.get(forecast_url, headers=headers, timeout=15)
        r2.raise_for_status()
        periods = r2.json()["properties"]["periods"]
        # Find the next daytime "isDaytime: true" period — that's tomorrow's high.
        for p in periods:
            if p.get("isDaytime"):
                # Skip today's remainder if there's a later daytime period within ~36h.
                temp = p.get("temperature")
                if temp is not None and p.get("temperatureUnit") == "F":
                    return float(temp)
        return None
    except Exception:
        return None
