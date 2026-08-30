from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from data.weather_client import WeatherState
from data.weather_station_mapper import StationMapping
from features.market_features import build_market_features
from features.weather_features import build_weather_features
from parsing.weather_contract import WeatherContract


class FeatureBuilder:
    def build(
        self,
        contract: WeatherContract,
        market: dict,
        weather_state: WeatherState,
        station_mapping: StationMapping,
        orderbook=None,
        as_of: datetime | None = None,
    ) -> dict:
        as_of = as_of or datetime.now(timezone.utc)
        local_dt = as_of.astimezone(ZoneInfo(station_mapping.timezone))
        features = {}
        features.update(build_weather_features(weather_state, local_dt))
        features.update(build_market_features(market, orderbook))
        threshold = contract.threshold
        features.update(
            {
                "threshold": threshold,
                "threshold_gap_current": _gap(threshold, features.get("current_temp")),
                "threshold_gap_max_so_far": _gap(threshold, features.get("max_temp_so_far")),
                "threshold_gap_forecast_high": _gap(threshold, features.get("forecast_high_remaining")),
                "threshold_gap_forecast_day": _gap(threshold, features.get("forecast_max_for_day")),
                "is_threshold_already_hit": _already_hit(contract, features),
                "time_to_close_minutes": _minutes_until(contract.close_time, as_of),
                "time_to_settlement_minutes": _minutes_until(contract.expiration_time, as_of),
                "parse_confidence": contract.parse_confidence,
                "station_confidence": station_mapping.confidence,
            }
        )
        return features


def _gap(threshold: float | None, value: float | None) -> float | None:
    if threshold is None or value is None:
        return None
    return threshold - value


def _minutes_until(timestamp, as_of: datetime) -> float | None:
    if timestamp is None:
        return None
    return (timestamp - as_of).total_seconds() / 60.0


def _already_hit(contract: WeatherContract, features: dict) -> bool:
    threshold = contract.threshold
    if threshold is None:
        return False
    if contract.variable_type == "high_temp":
        max_temp = features.get("max_temp_so_far")
        if max_temp is None:
            return False
        if contract.comparator == "gt":
            return max_temp > threshold
        if contract.comparator == "gte":
            return max_temp >= threshold
        if contract.comparator == "lt":
            return max_temp < threshold
        return max_temp <= threshold
    if contract.variable_type == "low_temp":
        min_temp = features.get("min_temp_so_far")
        if min_temp is None:
            return False
        if contract.comparator == "lt":
            return min_temp < threshold
        if contract.comparator == "lte":
            return min_temp <= threshold
        if contract.comparator == "gt":
            return min_temp > threshold
        return min_temp >= threshold
    return False
