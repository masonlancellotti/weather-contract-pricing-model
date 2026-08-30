from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import Settings, settings
from data.active_weather_station_resolver import ActiveWeatherStationResolver, unique_station_mappings
from data.storage import Storage
from data.weather_client import WeatherClient, WeatherObservation
from data.weather_station_mapper import StationMapper, StationMapping

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeatherRecorderResult:
    recorder_name: str
    started_at: datetime
    finished_at: datetime
    cycles: int
    stations: int
    rows_written: int
    failures: int
    warnings: list[str]

    def to_dict(self) -> dict:
        return {
            "recorder_name": self.recorder_name,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "cycles": self.cycles,
            "stations": self.stations,
            "rows_written": self.rows_written,
            "failures": self.failures,
            "warnings": self.warnings,
        }


class WeatherObservationRecorder:
    """Record live station observations in a separate process from orderbooks."""

    collector_name = "weather_observation_recorder"

    def __init__(self, storage: Storage | None = None, weather_client: WeatherClient | None = None, cfg: Settings = settings):
        self.storage = storage or Storage()
        self.weather_client = weather_client or WeatherClient()
        self.cfg = cfg
        self.mapper = StationMapper()
        self.resolver = ActiveWeatherStationResolver(storage=self.storage)

    def run(
        self,
        *,
        stations: list[str] | None = None,
        from_active_markets: bool = False,
        weather_only: bool = True,
        interval_minutes: int = 5,
        duration_hours: float | None = None,
        max_markets: int | None = None,
        once: bool = False,
    ) -> WeatherRecorderResult:
        started_at = datetime.now(timezone.utc)
        deadline = started_at + timedelta(hours=duration_hours) if duration_hours and duration_hours > 0 else None
        mappings = self._station_mappings(stations, from_active_markets, weather_only, max_markets)
        cycles = 0
        rows_written = 0
        failures = 0
        warnings: list[str] = []
        last_success_at: datetime | None = None
        LOGGER.info("weather observation recorder started stations=%s interval_minutes=%s", len(mappings), interval_minutes)
        self._update_state(started_at, None, 0, 0, len(mappings), "starting", "STARTING", None)
        try:
            while True:
                if deadline and datetime.now(timezone.utc) >= deadline:
                    break
                cycles += 1
                successful = 0
                failed = 0
                self._update_state(started_at, last_success_at, cycles, rows_written, len(mappings), "recording_observations", "RECORDING", None)
                for mapping in mappings:
                    try:
                        result = self.weather_client.latest_observation_payload(mapping.station_code)
                        if not result or not result[0]:
                            local_date = datetime.now(timezone.utc).astimezone(ZoneInfo(mapping.timezone)).date()
                            fallback = self.weather_client.historical_hourly_observations(mapping.station_code, local_date, mapping.timezone)
                            if not fallback:
                                failed += 1
                                failures += 1
                                continue
                            obs = fallback[-1]
                            payload = obs.to_dict()
                            source_url = "IEM/ASOS or historical hourly fallback"
                        else:
                            obs, payload, source_url = result
                        self.storage.insert_weather_observation_snapshot(_observation_row(mapping, obs, payload, source_url))
                        rows_written += 1
                        successful += 1
                        last_success_at = datetime.now(timezone.utc)
                    except Exception as exc:
                        failed += 1
                        failures += 1
                        message = f"{mapping.station_code}: observation fetch failed: {exc}"
                        warnings.append(message)
                        LOGGER.warning(message)
                self._heartbeat("WEATHER OBS HEARTBEAT", mappings, successful, failed, rows_written, last_success_at)
                if once:
                    break
                self._update_state(started_at, last_success_at, cycles, rows_written, len(mappings), "sleeping", "SLEEPING", None)
                time.sleep(max(interval_minutes, 1) * 60)
        except KeyboardInterrupt:
            LOGGER.info("weather observation recorder stopped by user")
        finished_at = datetime.now(timezone.utc)
        self._update_state(started_at, last_success_at, cycles, rows_written, len(mappings), "stopped", "STOPPED", None)
        return WeatherRecorderResult(self.collector_name, started_at, finished_at, cycles, len(mappings), rows_written, failures, warnings[-100:])

    def _station_mappings(self, stations: list[str] | None, from_active_markets: bool, weather_only: bool, max_markets: int | None) -> list[StationMapping]:
        if stations:
            return [self.mapper.resolve_station_code(station) for station in stations]
        if from_active_markets or weather_only:
            result = self.resolver.resolve_active(weather_only=weather_only, max_markets=max_markets, persist=True)
            return [_mapping_from_row(row) for row in unique_station_mappings(result.rows)]
        return []

    def _heartbeat(self, label: str, mappings: list[StationMapping], successful: int, failed: int, rows_written: int, last_success_at: datetime | None) -> None:
        age = (datetime.now(timezone.utc) - last_success_at).total_seconds() if last_success_at else None
        line = f"{label} stations={len(mappings)} successful={successful} failed={failed} rows_written={rows_written} last_success_age_sec={age if age is not None else 'none'}"
        LOGGER.info(line)
        print(line, flush=True)

    def _update_state(self, started_at: datetime, last_success_at: datetime | None, cycles: int, rows: int, stations: int, task: str, status: str, error: str | None) -> None:
        self.storage.upsert_collector_state(
            {
                "collector_name": self.collector_name,
                "started_at": started_at,
                "last_heartbeat_at": datetime.now(timezone.utc),
                "last_snapshot_at": last_success_at,
                "cycles_completed": cycles,
                "snapshots_this_run": rows,
                "markets_tracked": stations,
                "current_task": task,
                "status": status,
                "error_message": error,
                "updated_at": datetime.now(timezone.utc),
            }
        )


class WeatherForecastRecorder(WeatherObservationRecorder):
    """Record live forecast snapshots append-only for no-lookahead replay."""

    collector_name = "weather_forecast_recorder"

    def run(
        self,
        *,
        stations: list[str] | None = None,
        from_active_markets: bool = False,
        weather_only: bool = True,
        interval_minutes: int = 30,
        duration_hours: float | None = None,
        max_markets: int | None = None,
        once: bool = False,
    ) -> WeatherRecorderResult:
        started_at = datetime.now(timezone.utc)
        deadline = started_at + timedelta(hours=duration_hours) if duration_hours and duration_hours > 0 else None
        mappings = self._station_mappings(stations, from_active_markets, weather_only, max_markets)
        cycles = 0
        rows_written = 0
        failures = 0
        warnings: list[str] = []
        last_success_at: datetime | None = None
        LOGGER.info("weather forecast recorder started stations=%s interval_minutes=%s", len(mappings), interval_minutes)
        self._update_state(started_at, None, 0, 0, len(mappings), "starting", "STARTING", None)
        try:
            while True:
                if deadline and datetime.now(timezone.utc) >= deadline:
                    break
                cycles += 1
                successful = 0
                failed = 0
                self._update_state(started_at, last_success_at, cycles, rows_written, len(mappings), "recording_forecasts", "RECORDING", None)
                recorded_at = datetime.now(timezone.utc)
                for mapping in mappings:
                    try:
                        rows = self.weather_client.hourly_forecast_snapshot_rows(mapping, recorded_at=recorded_at)
                        if not rows:
                            failed += 1
                            failures += 1
                            continue
                        rows_written += self.storage.insert_weather_forecast_snapshots(rows)
                        successful += 1
                        last_success_at = datetime.now(timezone.utc)
                    except Exception as exc:
                        failed += 1
                        failures += 1
                        message = f"{mapping.station_code}: forecast fetch failed: {exc}"
                        warnings.append(message)
                        LOGGER.warning(message)
                self._heartbeat("WEATHER FCST HEARTBEAT", mappings, successful, failed, rows_written, last_success_at)
                if once:
                    break
                self._update_state(started_at, last_success_at, cycles, rows_written, len(mappings), "sleeping", "SLEEPING", None)
                time.sleep(max(interval_minutes, 1) * 60)
        except KeyboardInterrupt:
            LOGGER.info("weather forecast recorder stopped by user")
        finished_at = datetime.now(timezone.utc)
        self._update_state(started_at, last_success_at, cycles, rows_written, len(mappings), "stopped", "STOPPED", None)
        return WeatherRecorderResult(self.collector_name, started_at, finished_at, cycles, len(mappings), rows_written, failures, warnings[-100:])


def _mapping_from_row(row: dict) -> StationMapping:
    base = StationMapper().resolve_station_code(str(row.get("station_code") or ""))
    return StationMapping(
        city=str(row.get("city") or row.get("station_code") or ""),
        station_code=str(row.get("station_code") or "").upper(),
        timezone=str(row.get("timezone") or base.timezone or "America/New_York"),
        latitude=base.latitude,
        longitude=base.longitude,
        confidence=float(row.get("mapping_confidence") or 0.0),
        source=str(row.get("mapping_reason") or "active_market_map"),
        station_name=row.get("station_name") or base.station_name,
        wfo=row.get("wfo") or base.wfo,
        notes=row.get("warnings"),
    )


def _observation_row(mapping: StationMapping, obs: WeatherObservation, payload: dict, source_url: str) -> dict:
    props = payload.get("properties", payload)
    return {
        "station_code": mapping.station_code,
        "station_name": mapping.station_name,
        "ts_observed": obs.observed_at,
        "ts_recorded": datetime.now(timezone.utc),
        "source": obs.source,
        "source_url": source_url,
        "temp_f": obs.temp_f,
        "dewpoint_f": obs.dew_point_f,
        "humidity": obs.humidity,
        "wind_speed_mph": obs.wind_speed_mph,
        "wind_direction_degrees": obs.wind_direction,
        "wind_gust_mph": obs.wind_gust_mph,
        "pressure_mb": obs.pressure_hpa,
        "visibility_miles": _meters_to_miles(_payload_value(props.get("visibility"))),
        "precip_1h": _meters_to_inches(_payload_value(props.get("precipitationLastHour"))),
        "precip_3h": _meters_to_inches(_payload_value(props.get("precipitationLast3Hours"))),
        "raw_text": props.get("textDescription"),
        "raw_json": json.dumps(payload, default=str),
        "quality_score": 0.9 if obs.temp_f is not None else 0.6,
        "warnings": "Live observation feature source; final settlement still requires NWS Daily Climate Report where available.",
    }


def _payload_value(node: object) -> float | None:
    if isinstance(node, dict):
        node = node.get("value")
    if node is None:
        return None
    try:
        return float(node)
    except (TypeError, ValueError):
        return None


def _meters_to_miles(value: float | None) -> float | None:
    return value * 0.000621371 if value is not None else None


def _meters_to_inches(value: float | None) -> float | None:
    return value * 39.3701 if value is not None else None
