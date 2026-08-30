from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pandas as pd

from backtest.recorded_replay import recorded_weather_features_asof, weather_features_asof
from config import settings
from data.active_weather_station_resolver import ActiveWeatherStationResolver
from data.storage import Storage
from data.weather_client import WeatherClient
from data.weather_station_mapper import StationMapper
from live.weather_recorder import WeatherObservationRecorder
from parsing.weather_contract import WeatherContract


def _temp_storage(tmp_path) -> Storage:
    return Storage(replace(settings, database_url=f"sqlite:///{tmp_path / 'test.db'}"))


def test_active_weather_market_station_mapping_prefers_explicit_station():
    contract = WeatherContract(
        event_ticker="E",
        market_ticker="M",
        city="Philadelphia",
        station_code="KPHL",
        local_date=date(2026, 4, 30),
        variable_type="high_temp",
        contract_type="range_bucket",
        range_low=66,
        range_high=67,
        settlement_source="NWS Daily Climate Report",
        station_confidence=0.95,
        parse_confidence=0.95,
    )
    row = ActiveWeatherStationResolver()._row_for_contract(contract)
    assert row["station_code"] == "KPHL"
    assert row["mapping_reason"] == "explicit_station_from_rules"
    assert row["mapping_confidence"] >= 0.9


def test_station_mapper_has_midway_forecast_metadata():
    mapping = StationMapper().resolve_station_code("KMDW")
    assert mapping.station_code == "KMDW"
    assert mapping.latitude is not None
    assert mapping.longitude is not None
    assert mapping.timezone == "America/Chicago"


def test_weather_snapshot_inserts(tmp_path):
    storage = _temp_storage(tmp_path)
    storage.insert_weather_observation_snapshot(
        {
            "station_code": "KPHL",
            "station_name": "Philadelphia International",
            "ts_observed": datetime(2026, 4, 30, 18, tzinfo=timezone.utc),
            "ts_recorded": datetime(2026, 4, 30, 18, 1, tzinfo=timezone.utc),
            "source": "test",
            "temp_f": 66,
            "quality_score": 0.9,
        }
    )
    storage.insert_weather_forecast_snapshots(
        [
            {
                "station_code": "KPHL",
                "ts_recorded": datetime(2026, 4, 30, 18, 1, tzinfo=timezone.utc),
                "forecast_valid_start": datetime(2026, 4, 30, 19, tzinfo=timezone.utc),
                "source": "test_forecast",
                "forecast_hour": 0,
                "temp_f": 67,
                "quality_score": 0.8,
            }
        ]
    )
    assert len(storage.fetch_table("weather_observation_snapshots_live")) == 1
    assert len(storage.fetch_table("weather_forecast_snapshots_live")) == 1


def test_recorded_weather_asof_blocks_future_forecast_leakage():
    contract = WeatherContract(
        event_ticker="E",
        market_ticker="M",
        city="Philadelphia",
        station_code="KPHL",
        local_date=date(2026, 4, 30),
        variable_type="high_temp",
        contract_type="threshold_above",
        threshold=70,
        comparator="gte",
    )
    observations = pd.DataFrame(
        [
            {
                "station_code": "KPHL",
                "ts_observed": datetime(2026, 4, 30, 18, tzinfo=timezone.utc),
                "ts_recorded": datetime(2026, 4, 30, 18, 1, tzinfo=timezone.utc),
                "temp_f": 65,
                "quality_score": 0.9,
            }
        ]
    )
    forecasts = pd.DataFrame(
        [
            {
                "station_code": "KPHL",
                "ts_recorded": datetime(2026, 4, 30, 18, 5, tzinfo=timezone.utc),
                "forecast_valid_start": datetime(2026, 4, 30, 20, tzinfo=timezone.utc),
                "source": "future_forecast",
                "temp_f": 90,
                "quality_score": 0.9,
            },
            {
                "station_code": "KPHL",
                "ts_recorded": datetime(2026, 4, 30, 17, 55, tzinfo=timezone.utc),
                "forecast_valid_start": datetime(2026, 4, 30, 20, tzinfo=timezone.utc),
                "source": "known_forecast",
                "temp_f": 68,
                "quality_score": 0.8,
            },
        ]
    )
    features = recorded_weather_features_asof(contract, observations, forecasts, "America/New_York", datetime(2026, 4, 30, 18, 2, tzinfo=timezone.utc))
    assert features["weather_feature_source"] == "recorded_live_asof"
    assert features["forecast_high_remaining_f"] == 68
    assert features["forecast_source"] == "known_forecast"


def test_missing_weather_data_does_not_crash_feature_builder():
    contract = WeatherContract(event_ticker="E", market_ticker="M", threshold=70, comparator="gte")
    features = weather_features_asof(contract, [], "America/New_York", datetime(2026, 4, 30, 18, tzinfo=timezone.utc))
    assert features["current_temp_asof"] is None
    assert features["observations_count_so_far"] == 0


class FailingWeatherClient(WeatherClient):
    def latest_observation_payload(self, station_code: str):
        raise RuntimeError("weather source down")


def test_weather_recorder_failure_does_not_touch_orderbook_recorder(tmp_path):
    storage = _temp_storage(tmp_path)
    result = WeatherObservationRecorder(storage=storage, weather_client=FailingWeatherClient()).run(stations=["KPHL"], once=True)
    assert result.failures == 1
    assert len(storage.fetch_table("orderbook_snapshots_live")) == 0
