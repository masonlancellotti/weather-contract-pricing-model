from datetime import date, datetime, timezone

from backtest.recorded_replay import _weather_cache_ts
from backtest.replay_builder import _weather_features_asof
from data.weather_client import WeatherObservation
from parsing.weather_contract import WeatherContract


def test_replay_weather_features_use_only_observations_at_or_before_timestamp():
    contract = WeatherContract(
        event_ticker="E",
        market_ticker="M",
        local_date=date(2026, 4, 27),
        variable_type="high_temp",
        threshold=90,
        comparator="gte",
    )
    observations = [
        WeatherObservation("KNYC", datetime(2026, 4, 27, 16, tzinfo=timezone.utc), temp_f=80),
        WeatherObservation("KNYC", datetime(2026, 4, 27, 18, tzinfo=timezone.utc), temp_f=95),
    ]
    features = _weather_features_asof(contract, observations, "America/New_York", datetime(2026, 4, 27, 17, tzinfo=timezone.utc))
    assert features["max_temp_so_far_asof"] == 80
    assert features["is_threshold_already_hit_asof"] is False


def test_recorded_replay_weather_cache_timestamp_floors_without_lookahead():
    ts = datetime(2026, 5, 1, 12, 7, 45, tzinfo=timezone.utc)
    cached = _weather_cache_ts(ts)
    assert cached == datetime(2026, 5, 1, 12, 5, tzinfo=timezone.utc)
    assert cached <= ts
