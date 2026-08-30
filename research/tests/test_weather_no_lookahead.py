from datetime import date, datetime, timezone

from data.weather_client import WeatherClient, WeatherObservation
from data.weather_station_mapper import StationMapping


class FakeWeatherClient(WeatherClient):
    def hourly_observations(self, station_code: str, local_date: date, timezone_name: str):
        return []

    def latest_observation(self, station_code: str):
        return WeatherObservation(
            station_code=station_code,
            observed_at=datetime(2026, 4, 27, 16, tzinfo=timezone.utc),
            temp_f=95.0,
            source="test",
        )

    def open_meteo_forecast(self, mapping: StationMapping, local_date: date):
        return {}


def test_future_contract_does_not_use_today_observation_as_so_far_value():
    client = FakeWeatherClient()
    mapping = StationMapping("New York", "KNYC", "America/New_York")
    state = client.weather_state(mapping, date(2026, 4, 28), as_of=datetime(2026, 4, 27, 17, tzinfo=timezone.utc))
    assert state.current_temp is None
    assert state.max_temp_so_far is None
    assert state.min_temp_so_far is None
