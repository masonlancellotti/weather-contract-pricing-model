from __future__ import annotations

import logging
import csv
import json
import random
import re
import time
from io import StringIO
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from config import settings
from data.weather_station_mapper import StationMapping

LOGGER = logging.getLogger(__name__)


def c_to_f(value: float | None) -> float | None:
    if value is None:
        return None
    return value * 9.0 / 5.0 + 32.0


@dataclass(frozen=True)
class WeatherObservation:
    station_code: str
    observed_at: datetime
    temp_f: float | None = None
    dew_point_f: float | None = None
    humidity: float | None = None
    wind_speed_mph: float | None = None
    wind_direction: float | None = None
    wind_gust_mph: float | None = None
    pressure_hpa: float | None = None
    source: str = "unknown"

    def to_dict(self) -> dict:
        payload = self.__dict__.copy()
        payload["observed_at"] = self.observed_at.isoformat()
        return payload


@dataclass(frozen=True)
class WeatherState:
    station_code: str
    as_of: datetime
    current_temp: float | None
    max_temp_so_far: float | None
    min_temp_so_far: float | None
    temp_1h_ago: float | None
    temp_3h_ago: float | None
    temp_trend_1h: float | None
    temp_trend_3h: float | None
    forecast_high_remaining: float | None
    forecast_low_remaining: float | None
    forecast_max_for_day: float | None
    forecast_min_for_day: float | None
    data_quality_score: float
    data_age_minutes: float | None
    warnings: list[str]

    def to_dict(self) -> dict:
        payload = self.__dict__.copy()
        payload["as_of"] = self.as_of.isoformat()
        return payload


class WeatherClient:
    """Weather data client with NWS observations first and Open-Meteo forecast fallback."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-weather-edge/0.1 research bot"})
        self._cache: dict[str, tuple[float, object]] = {}
        self.stats: dict[str, object] = {
            "weather_api_errors_total": 0,
            "weather_api_timeouts_total": 0,
            "station_error_counts": {},
            "last_successful_weather_fetch_by_station": {},
        }

    def latest_observation(self, station_code: str) -> WeatherObservation | None:
        result = self.latest_observation_payload(station_code)
        return result[0] if result else None

    def latest_observation_payload(self, station_code: str) -> tuple[WeatherObservation | None, dict, str] | None:
        station = station_code.upper()
        url = f"https://api.weather.gov/stations/{station}/observations/latest"
        try:
            response = self._get(url, station=station)
            if response.status_code >= 400:
                LOGGER.warning("NWS latest observation failed for %s: %s", station, response.status_code)
                return None
            payload = response.json()
            return _nws_observation_to_model(station, payload), payload, response.url
        except requests.RequestException as exc:
            LOGGER.warning("NWS latest observation error for %s: %s", station, exc)
            return None

    def hourly_observations(self, station_code: str, local_date: date, timezone_name: str) -> list[WeatherObservation]:
        station = station_code.upper()
        tz = ZoneInfo(timezone_name)
        start = datetime.combine(local_date, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        if start > now:
            return []
        end = min(now, start + timedelta(days=1))
        url = f"https://api.weather.gov/stations/{station}/observations"
        params = {"start": start.isoformat().replace("+00:00", "Z"), "end": end.isoformat().replace("+00:00", "Z")}
        try:
            response = self._get(url, params=params, station=station)
            if response.status_code >= 400:
                LOGGER.warning("NWS observations failed for %s: %s", station, response.status_code)
                return []
            features = response.json().get("features", [])
            obs = [_nws_observation_to_model(station, item) for item in features]
            return sorted([item for item in obs if item], key=lambda item: item.observed_at)
        except requests.RequestException as exc:
            LOGGER.warning("NWS observations error for %s: %s", station, exc)
            return []

    def historical_hourly_observations(self, station_code: str, local_date: date, timezone_name: str) -> list[WeatherObservation]:
        observations = self.hourly_observations(station_code, local_date, timezone_name)
        if observations:
            return observations
        return self.iem_asos_observations(station_code, local_date, timezone_name)

    def iem_asos_observations(self, station_code: str, local_date: date, timezone_name: str) -> list[WeatherObservation]:
        station = station_code.upper().removeprefix("K")
        tz = ZoneInfo(timezone_name)
        start_utc = datetime.combine(local_date, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        end_utc = (datetime.combine(local_date, datetime.min.time(), tzinfo=tz) + timedelta(days=1)).astimezone(timezone.utc)
        url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        params = {
            "station": station,
            "data": "tmpf",
            "year1": start_utc.year,
            "month1": start_utc.month,
            "day1": start_utc.day,
            "year2": end_utc.year,
            "month2": end_utc.month,
            "day2": end_utc.day,
            "tz": "Etc/UTC",
            "format": "onlycomma",
            "latlon": "no",
            "elev": "no",
            "missing": "M",
            "trace": "T",
            "direct": "no",
            "report_type": ["1", "2"],
        }
        try:
            response = self._get(url, params=params, station=station_code.upper())
            if response.status_code >= 400:
                LOGGER.warning("IEM ASOS observations failed for %s: %s", station_code, response.status_code)
                return []
            rows = csv.DictReader(StringIO(response.text))
            observations: list[WeatherObservation] = []
            for row in rows:
                if row.get("tmpf") in {None, "", "M"}:
                    continue
                try:
                    observed_at = datetime.fromisoformat(str(row["valid"]).replace("Z", "+00:00"))
                    if observed_at.tzinfo is None:
                        observed_at = observed_at.replace(tzinfo=timezone.utc)
                    temp_f = float(row["tmpf"])
                except (KeyError, TypeError, ValueError):
                    continue
                if observed_at.astimezone(tz).date() != local_date:
                    continue
                observations.append(WeatherObservation(station_code=station_code.upper(), observed_at=observed_at, temp_f=temp_f, source="IEM_ASOS"))
            return sorted(observations, key=lambda item: item.observed_at)
        except requests.RequestException as exc:
            LOGGER.warning("IEM ASOS observations error for %s: %s", station_code, exc)
            return []

    def open_meteo_forecast(self, mapping: StationMapping, local_date: date) -> dict:
        if mapping.latitude is None or mapping.longitude is None:
            return {}
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": mapping.latitude,
            "longitude": mapping.longitude,
            "hourly": "temperature_2m",
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "fahrenheit",
            "timezone": mapping.timezone,
            "start_date": local_date.isoformat(),
            "end_date": local_date.isoformat(),
        }
        try:
            response = self._get(url, params=params, station=mapping.station_code)
            if response.status_code >= 400:
                LOGGER.warning("Open-Meteo forecast failed for %s: %s", mapping.station_code, response.status_code)
                return {}
            return response.json()
        except requests.RequestException as exc:
            LOGGER.warning("Open-Meteo forecast error for %s: %s", mapping.station_code, exc)
            return {}

    def hourly_forecast_snapshot_rows(self, mapping: StationMapping, recorded_at: datetime | None = None) -> list[dict[str, Any]]:
        """Fetch hourly forecast rows for one station.

        NWS gridpoint hourly forecast is preferred. Open-Meteo is feature-only
        fallback and is explicitly marked lower confidence.
        """
        recorded_at = recorded_at or datetime.now(timezone.utc)
        if mapping.latitude is not None and mapping.longitude is not None:
            rows = self._nws_hourly_forecast_rows(mapping, recorded_at)
            if rows:
                return rows
        return self._open_meteo_hourly_forecast_rows(mapping, recorded_at)

    def _nws_hourly_forecast_rows(self, mapping: StationMapping, recorded_at: datetime) -> list[dict[str, Any]]:
        if mapping.latitude is None or mapping.longitude is None:
            return []
        point_url = f"https://api.weather.gov/points/{mapping.latitude:.4f},{mapping.longitude:.4f}"
        try:
            point_response = self._get(point_url, station=mapping.station_code)
            if point_response.status_code >= 400:
                return []
            point_payload = point_response.json()
            hourly_url = point_payload.get("properties", {}).get("forecastHourly")
            if not hourly_url:
                return []
            forecast_response = self._get(hourly_url, station=mapping.station_code)
            if forecast_response.status_code >= 400:
                return []
            payload = forecast_response.json()
        except requests.RequestException as exc:
            LOGGER.warning("NWS hourly forecast error for %s: %s", mapping.station_code, exc)
            return []
        props = payload.get("properties", {})
        generated_at = _parse_datetime(props.get("generatedAt")) or recorded_at
        rows: list[dict[str, Any]] = []
        for idx, period in enumerate(props.get("periods", []) or []):
            start = _parse_datetime(period.get("startTime"))
            end = _parse_datetime(period.get("endTime"))
            rows.append(
                {
                    "station_code": mapping.station_code,
                    "station_name": mapping.station_name,
                    "ts_forecast_created": generated_at,
                    "ts_recorded": recorded_at,
                    "forecast_valid_start": start,
                    "forecast_valid_end": end,
                    "source": "nws_hourly_forecast",
                    "source_url": forecast_response.url,
                    "forecast_hour": idx,
                    "temp_f": _float_or_none(period.get("temperature")),
                    "dewpoint_f": c_to_f(_value(period.get("dewpoint"))),
                    "humidity": _value(period.get("relativeHumidity")),
                    "wind_speed_mph": _parse_speed_mph(period.get("windSpeed")),
                    "wind_direction_degrees": None,
                    "precip_probability": _value(period.get("probabilityOfPrecipitation")),
                    "quantitative_precip": _value(period.get("quantitativePrecipitation")),
                    "sky_cover": None,
                    "raw_json": json.dumps(period, default=str),
                    "raw_text": period.get("shortForecast"),
                    "quality_score": 0.9,
                    "warnings": "NWS hourly forecast feature source; not settlement truth.",
                }
            )
        return rows

    def _open_meteo_hourly_forecast_rows(self, mapping: StationMapping, recorded_at: datetime) -> list[dict[str, Any]]:
        today = recorded_at.astimezone(ZoneInfo(mapping.timezone)).date()
        payload = self.open_meteo_forecast(mapping, today)
        hourly = payload.get("hourly", {}) if payload else {}
        times = hourly.get("time", []) or []
        temps = hourly.get("temperature_2m", []) or []
        rows: list[dict[str, Any]] = []
        for idx, (ts, temp) in enumerate(zip(times, temps, strict=False)):
            try:
                local_ts = datetime.fromisoformat(str(ts)).replace(tzinfo=ZoneInfo(mapping.timezone))
            except ValueError:
                continue
            rows.append(
                {
                    "station_code": mapping.station_code,
                    "station_name": mapping.station_name,
                    "ts_forecast_created": recorded_at,
                    "ts_recorded": recorded_at,
                    "forecast_valid_start": local_ts.astimezone(timezone.utc),
                    "forecast_valid_end": (local_ts + timedelta(hours=1)).astimezone(timezone.utc),
                    "source": "open_meteo_forecast_feature",
                    "source_url": "https://api.open-meteo.com/v1/forecast",
                    "forecast_hour": idx,
                    "temp_f": _float_or_none(temp),
                    "dewpoint_f": None,
                    "humidity": None,
                    "wind_speed_mph": None,
                    "wind_direction_degrees": None,
                    "precip_probability": None,
                    "quantitative_precip": None,
                    "sky_cover": None,
                    "raw_json": json.dumps({"time": ts, "temperature_2m": temp}, default=str),
                    "raw_text": None,
                    "quality_score": 0.65,
                    "warnings": "Open-Meteo forecast feature source; not settlement truth.",
                }
            )
        return rows

    def _get(self, url: str, params: dict | None = None, station: str | None = None) -> requests.Response:
        cache_key = json_cache_key(url, params)
        cached = self._cache.get(cache_key)
        now = time.time()
        if cached and now - cached[0] <= settings.weather_cache_ttl_seconds:
            return cached[1]  # type: ignore[return-value]
        last_error: requests.RequestException | None = None
        for attempt in range(max(1, settings.nws_max_retries)):
            try:
                response = self.session.get(url, params=params, timeout=settings.nws_timeout_seconds)
                self._cache[cache_key] = (time.time(), response)
                if station:
                    self.stats["last_successful_weather_fetch_by_station"][station] = datetime.now(timezone.utc).isoformat()  # type: ignore[index]
                return response
            except requests.Timeout as exc:
                self.stats["weather_api_timeouts_total"] = int(self.stats["weather_api_timeouts_total"]) + 1
                last_error = exc
            except requests.RequestException as exc:
                self.stats["weather_api_errors_total"] = int(self.stats["weather_api_errors_total"]) + 1
                last_error = exc
            if station:
                counts = self.stats["station_error_counts"]  # type: ignore[assignment]
                counts[station] = counts.get(station, 0) + 1
            sleep_seconds = min(2.0 * (2**attempt), float(settings.nws_backoff_max_seconds)) + random.uniform(0.0, 1.0)
            time.sleep(sleep_seconds)
        raise last_error or requests.RequestException("weather request failed")

    def weather_state(self, mapping: StationMapping, local_date: date, as_of: datetime | None = None) -> WeatherState:
        as_of = as_of or datetime.now(timezone.utc)
        observations = self.hourly_observations(mapping.station_code, local_date, mapping.timezone)
        latest = observations[-1] if observations else self.latest_observation(mapping.station_code)
        tz = ZoneInfo(mapping.timezone)
        if latest and latest.observed_at.astimezone(tz).date() != local_date:
            latest = None
        if latest and (not observations or latest.observed_at > observations[-1].observed_at):
            observations = [*observations, latest]
        temps = [obs.temp_f for obs in observations if obs.temp_f is not None and obs.observed_at <= as_of]
        current_temp = latest.temp_f if latest else None
        max_so_far = max(temps) if temps else current_temp
        min_so_far = min(temps) if temps else current_temp
        temp_1h_ago = _temp_near(observations, as_of - timedelta(hours=1))
        temp_3h_ago = _temp_near(observations, as_of - timedelta(hours=3))
        data_age = ((as_of - latest.observed_at).total_seconds() / 60.0) if latest else None
        forecast = self.open_meteo_forecast(mapping, local_date)
        daily = forecast.get("daily", {}) if forecast else {}
        hourly = forecast.get("hourly", {}) if forecast else {}
        hourly_times = hourly.get("time", [])
        hourly_temps = hourly.get("temperature_2m", [])
        remaining = []
        for ts, temp in zip(hourly_times, hourly_temps, strict=False):
            try:
                local_ts = datetime.fromisoformat(ts).replace(tzinfo=ZoneInfo(mapping.timezone))
            except ValueError:
                continue
            if local_ts.astimezone(timezone.utc) >= as_of:
                remaining.append(float(temp))
        warnings: list[str] = []
        if latest is None:
            warnings.append("no current NWS observation")
        if not observations:
            warnings.append("no intraday station observations")
        if not forecast:
            warnings.append("no Open-Meteo forecast fallback")
        quality = 1.0
        if latest is None:
            quality -= 0.4
        if not observations:
            quality -= 0.2
        if not forecast:
            quality -= 0.2
        if data_age is not None and data_age > 20:
            quality -= 0.2
            warnings.append(f"weather data stale: {data_age:.1f} minutes")
        quality = max(0.0, min(1.0, quality))
        return WeatherState(
            station_code=mapping.station_code,
            as_of=as_of,
            current_temp=current_temp,
            max_temp_so_far=max_so_far,
            min_temp_so_far=min_so_far,
            temp_1h_ago=temp_1h_ago,
            temp_3h_ago=temp_3h_ago,
            temp_trend_1h=(current_temp - temp_1h_ago) if current_temp is not None and temp_1h_ago is not None else None,
            temp_trend_3h=(current_temp - temp_3h_ago) if current_temp is not None and temp_3h_ago is not None else None,
            forecast_high_remaining=max(remaining) if remaining else None,
            forecast_low_remaining=min(remaining) if remaining else None,
            forecast_max_for_day=(daily.get("temperature_2m_max") or [None])[0] if daily else None,
            forecast_min_for_day=(daily.get("temperature_2m_min") or [None])[0] if daily else None,
            data_quality_score=quality,
            data_age_minutes=data_age,
            warnings=warnings,
        )


def _nws_observation_to_model(station: str, payload: dict) -> WeatherObservation | None:
    props = payload.get("properties", payload)
    timestamp = props.get("timestamp")
    if not timestamp:
        return None
    try:
        observed_at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    temp_c = _value(props.get("temperature"))
    dew_c = _value(props.get("dewpoint"))
    wind_kmh = _value(props.get("windSpeed"))
    gust_kmh = _value(props.get("windGust"))
    pressure_pa = _value(props.get("barometricPressure"))
    return WeatherObservation(
        station_code=station,
        observed_at=observed_at,
        temp_f=c_to_f(temp_c),
        dew_point_f=c_to_f(dew_c),
        humidity=_value(props.get("relativeHumidity")),
        wind_speed_mph=wind_kmh * 0.621371 if wind_kmh is not None else None,
        wind_direction=_value(props.get("windDirection")),
        wind_gust_mph=gust_kmh * 0.621371 if gust_kmh is not None else None,
        pressure_hpa=pressure_pa / 100.0 if pressure_pa is not None else None,
        source="NWS",
    )


def _value(node: object) -> float | None:
    if isinstance(node, dict):
        node = node.get("value")
    if node is None:
        return None
    try:
        return float(node)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_speed_mph(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def _temp_near(observations: list[WeatherObservation], target: datetime) -> float | None:
    candidates = [obs for obs in observations if obs.temp_f is not None and obs.observed_at <= target]
    if not candidates:
        return None
    return min(candidates, key=lambda obs: abs((target - obs.observed_at).total_seconds())).temp_f


def json_cache_key(url: str, params: dict | None) -> str:
    return json.dumps({"url": url, "params": params or {}}, sort_keys=True, default=str)
