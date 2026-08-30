"""
UTC = timezone.utc

weather/providers.py — Weather data ingestion.

Sources:
  1. NWS API (api.weather.gov) — current obs + hourly forecast, no auth needed
  2. IEM ASOS (mesonet.agron.iastate.edu) — historical daily obs, no auth
  3. NOAA CDO (ncdc.noaa.gov) — official daily max/min, requires free token
  4. Open-Meteo (open-meteo.com) — forecast fallback, no auth

All temperatures normalized to °F.

IMPORTANT: NWS obs API returns °C — we convert. NWS forecast returns °F.
Known NWS bug: maxTemperatureLast24Hours is always null outside Central TZ.
We work around this by fetching recent hourly obs and taking max ourselves.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta, date, timezone
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from models import WeatherForecast, WeatherObs
from utils.time import city_local_now
from weather.mapping import CityMeta

UTC = timezone.utc

log = logging.getLogger("weather.providers")

NWS_BASE = "https://api.weather.gov"
IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
CDO_BASE = "https://www.ncei.noaa.gov/cdo-web/api/v2"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

NWS_HEADERS = {"User-Agent": "weather-trader-bot/1.0 (contact@example.com)"}

HISTORY_CACHE_TTL_SECONDS = 1800
HISTORY_CACHE_EMPTY_TTL_SECONDS = 300
IEM_RATE_LIMIT_BACKOFF_SECONDS = 21600
_history_cache: dict[tuple[str, str, int], tuple[float, list[dict]]] = {}
_iem_backoff_until: float = 0.0


def _c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True,
)
def _get(url: str, params: dict | None = None, headers: dict | None = None, timeout: float = 15.0) -> dict:
    r = httpx.get(url, params=params or {}, headers=headers or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ── NWS current observations ─────────────────────────────────────────────────

def get_nws_obs(city_meta: CityMeta) -> Optional[WeatherObs]:
    """Fetch latest observation from NWS for the settlement station."""
    url = f"{NWS_BASE}/stations/{city_meta.nws_station}/observations/latest"
    try:
        data = _get(url, headers=NWS_HEADERS)
        props = data.get("properties", {})
        temp_c = props.get("temperature", {}).get("value")
        if temp_c is None:
            log.warning("NWS obs: null temperature for %s", city_meta.nws_station)
            return None
        obs_time_str = props.get("timestamp", "")
        obs_time = datetime.fromisoformat(obs_time_str.replace("Z", "+00:00")) if obs_time_str else datetime.now(UTC)
        return WeatherObs(
            station_id=city_meta.nws_station,
            city=city_meta.city_code,
            temp_f=round(_c_to_f(float(temp_c)), 1),
            obs_time=obs_time,
            source="nws_obs",
        )
    except Exception as e:
        log.warning("NWS obs failed for %s: %s", city_meta.nws_station, e)
        return None


def get_nws_observed_high_so_far(
    city_meta: CityMeta,
    target_date: str | None = None,
    now_utc: datetime | None = None,
) -> Optional[float]:
    """
    Fetch the max observed temperature for the city-local day so far.
    """
    import pytz

    local_now = city_local_now(city_meta.city_code, now_utc)
    local_date = target_date or local_now.date().isoformat()
    tz = pytz.timezone(city_meta.timezone)
    start_local = tz.localize(datetime.fromisoformat(f"{local_date}T00:00:00"))
    end_local = min(local_now, start_local + timedelta(days=1))
    if end_local <= start_local:
        return None

    url = f"{NWS_BASE}/stations/{city_meta.nws_station}/observations"
    try:
        data = _get(
            url,
            params={
                "start": start_local.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "end": end_local.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "limit": 500,
            },
            headers=NWS_HEADERS,
        )
        temps_f: list[float] = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            temp_c = props.get("temperature", {}).get("value")
            if temp_c is None:
                continue
            temps_f.append(round(_c_to_f(float(temp_c)), 1))
        return max(temps_f) if temps_f else None
    except Exception as e:
        log.warning("NWS intraday obs failed for %s: %s", city_meta.nws_station, e)
        return None


# ── NWS gridpoint hourly forecast ────────────────────────────────────────────

def _get_nws_forecast_url(city_meta: CityMeta) -> Optional[str]:
    """Get the hourly forecast URL for a lat/lon from NWS points API."""
    try:
        data = _get(
            f"{NWS_BASE}/points/{city_meta.lat},{city_meta.lon}",
            headers=NWS_HEADERS,
        )
        return data["properties"]["forecastHourly"]
    except Exception as e:
        log.warning("NWS points lookup failed for %s: %s", city_meta.city_code, e)
        return None


def get_nws_forecast(city_meta: CityMeta, settlement_date: str) -> Optional[WeatherForecast]:
    """Fetch NWS hourly forecast and compute daily high for the contract settlement date."""
    forecast_url = _get_nws_forecast_url(city_meta)
    if not forecast_url:
        return _get_openmeteo_forecast(city_meta, settlement_date)

    try:
        data = _get(forecast_url, headers=NWS_HEADERS)
        periods = data.get("properties", {}).get("periods", [])
        if not periods:
            return _get_openmeteo_forecast(city_meta, settlement_date)

        import pytz
        tz = pytz.timezone(city_meta.timezone)

        temps_by_date: dict[str, list[float]] = {}
        hourly: list[dict] = []

        for period in periods:
            start = datetime.fromisoformat(period["startTime"].replace("Z", "+00:00")).astimezone(tz)
            temp_f = float(period.get("temperature", 0))
            if period.get("temperatureUnit") == "C":
                temp_f = _c_to_f(temp_f)

            hour_str = start.isoformat()
            hourly.append({"hour": hour_str, "temp_f": temp_f})

            day_key = start.date().isoformat()
            temps_by_date.setdefault(day_key, []).append(temp_f)

        if settlement_date in temps_by_date:
            target_date = settlement_date
        else:
            available_dates = sorted(temps_by_date.keys())
            if not available_dates:
                return _get_openmeteo_forecast(city_meta, settlement_date)
            target_date = next((d for d in available_dates if d >= settlement_date), available_dates[-1])

        temps = temps_by_date.get(target_date)
        if not temps:
            return _get_openmeteo_forecast(city_meta, settlement_date)

        high = max(temps)
        low = min(temps)
        return WeatherForecast(
            station_id=city_meta.nws_station,
            city=city_meta.city_code,
            forecast_for=target_date,
            high_f=high,
            low_f=low,
            hourly=hourly[:48],
            source="nws_gridpoint",
        )
    except Exception as e:
        log.warning("NWS hourly forecast failed for %s: %s", city_meta.city_code, e)
        return _get_openmeteo_forecast(city_meta, settlement_date)


def _get_openmeteo_forecast(city_meta: CityMeta, settlement_date: str) -> Optional[WeatherForecast]:
    """Open-Meteo fallback — free, no auth, uses NOAA HRRR for US."""
    try:
        data = _get(OPEN_METEO_BASE, params={
            "latitude": city_meta.lat,
            "longitude": city_meta.lon,
            "hourly": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "forecast_days": 3,
            "timezone": city_meta.timezone,
        })
        hours = data.get("hourly", {})
        times = hours.get("time", [])
        temps = hours.get("temperature_2m", [])
        if not times or not temps:
            return None

        day_temps: dict[str, list[float]] = {}
        hourly: list[dict] = []
        for t_str, temp in zip(times, temps):
            if temp is None:
                continue
            t = datetime.fromisoformat(t_str)
            d = t.date().isoformat()
            day_temps.setdefault(d, []).append(float(temp))
            hourly.append({"hour": t_str, "temp_f": float(temp)})

        if settlement_date in day_temps:
            target = settlement_date
        else:
            available_dates = sorted(day_temps.keys())
            if not available_dates:
                return None
            target = next((d for d in available_dates if d >= settlement_date), available_dates[-1])

        day = day_temps[target]
        return WeatherForecast(
            station_id=city_meta.nws_station,
            city=city_meta.city_code,
            forecast_for=target,
            high_f=max(day),
            low_f=min(day),
            hourly=hourly[:48],
            source="open_meteo",
        )
    except Exception as e:
        log.warning("Open-Meteo forecast failed for %s: %s", city_meta.city_code, e)
        return None


# ── IEM ASOS historical observations ─────────────────────────────────────────

def get_iem_daily_highs(station: str, days_back: int = 60) -> list[dict]:
    """
    Fetch recent daily high temps from IEM ASOS.
    Returns list of {date: YYYY-MM-DD, high_f: float}.
    Cached to avoid hammering IEM every strategy cycle.
    """
    cache_key = ("iem", station, days_back)
    now_ts = time.time()
    global _iem_backoff_until
    if now_ts < _iem_backoff_until:
        return []
    cached = _history_cache.get(cache_key)
    if cached and now_ts < cached[0]:
        return [dict(x) for x in cached[1]]

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)
    try:
        r = httpx.get(IEM_BASE, params={
            "station": station,
            "data": "tmpf",
            "year1": start.year, "month1": start.month, "day1": start.day,
            "year2": end.year, "month2": end.month, "day2": end.day,
            "tz": "UTC",
            "format": "comma",
            "latlon": "no",
            "missing": "M",
            "trace": "T",
            "direct": "no",
            "report_type": "3",
        }, timeout=30)
        r.raise_for_status()
        lines = r.text.strip().split("\n")
        daily: dict[str, list[float]] = {}
        for line in lines:
            if line.startswith("#") or line.startswith("station"):
                continue
            parts = line.split(",")
            if len(parts) < 3:
                continue
            date_str = parts[1][:10]
            try:
                temp_f = float(parts[2])
                daily.setdefault(date_str, []).append(temp_f)
            except ValueError:
                continue

        result = [
            {"date": d, "high_f": max(temps)}
            for d, temps in sorted(daily.items())
            if temps
        ]
        ttl = HISTORY_CACHE_TTL_SECONDS if result else HISTORY_CACHE_EMPTY_TTL_SECONDS
        _history_cache[cache_key] = (now_ts + ttl, result)
        return [dict(x) for x in result]
    except Exception as e:
        log.warning("IEM ASOS fetch failed for %s: %s", station, e)
        if isinstance(e, httpx.HTTPStatusError) and e.response is not None and e.response.status_code == 429:
            _iem_backoff_until = now_ts + IEM_RATE_LIMIT_BACKOFF_SECONDS
            log.warning("IEM rate limited - backing off requests for %.1f hours", IEM_RATE_LIMIT_BACKOFF_SECONDS / 3600)
        _history_cache[cache_key] = (now_ts + HISTORY_CACHE_EMPTY_TTL_SECONDS, [])
        return []


# ── NOAA CDO historical daily summaries ──────────────────────────────────────

def get_cdo_daily_highs(station_id: str, days_back: int = 60) -> list[dict]:
    """
    Fetch official daily max temps from NOAA CDO (GHCND).
    Cached to avoid repeating the same request every cycle.
    """
    from config import cfg

    cache_key = ("cdo", station_id, days_back)
    now_ts = time.time()
    cached = _history_cache.get(cache_key)
    if cached and now_ts < cached[0]:
        return [dict(x) for x in cached[1]]

    if not cfg.noaa_cdo_token:
        log.debug("No NOAA CDO token — skipping CDO fetch")
        _history_cache[cache_key] = (now_ts + HISTORY_CACHE_EMPTY_TTL_SECONDS, [])
        return []

    if not station_id.startswith("GHCND:"):
        log.debug("No valid CDO station id for %s — skipping CDO fetch", station_id)
        _history_cache[cache_key] = (now_ts + HISTORY_CACHE_EMPTY_TTL_SECONDS, [])
        return []

    end = date.today()
    start = end - timedelta(days=days_back)

    try:
        data = _get(
            f"{CDO_BASE}/data",
            params={
                "datasetid": "GHCND",
                "stationid": station_id,
                "startdate": start.isoformat(),
                "enddate": end.isoformat(),
                "datatypeid": "TMAX",
                "units": "standard",
                "limit": 1000,
            },
            headers={
                "token": cfg.noaa_cdo_token,
            },
        )
        result = [
            {"date": r["date"][:10], "high_f": float(r["value"])}
            for r in data.get("results", [])
        ]
        ttl = HISTORY_CACHE_TTL_SECONDS if result else HISTORY_CACHE_EMPTY_TTL_SECONDS
        _history_cache[cache_key] = (now_ts + ttl, result)
        return [dict(x) for x in result]
    except Exception as e:
        log.warning("NOAA CDO fetch failed for %s: %s", station_id, e)
        _history_cache[cache_key] = (now_ts + HISTORY_CACHE_EMPTY_TTL_SECONDS, [])
        return []
