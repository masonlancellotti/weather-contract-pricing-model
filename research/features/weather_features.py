from __future__ import annotations

from datetime import datetime

from data.weather_client import WeatherState


def build_weather_features(state: WeatherState, local_dt: datetime | None = None) -> dict:
    local_dt = local_dt or state.as_of
    month = local_dt.month
    return {
        "current_temp": state.current_temp,
        "max_temp_so_far": state.max_temp_so_far,
        "min_temp_so_far": state.min_temp_so_far,
        "temp_1h_ago": state.temp_1h_ago,
        "temp_3h_ago": state.temp_3h_ago,
        "temp_trend_1h": state.temp_trend_1h,
        "temp_trend_3h": state.temp_trend_3h,
        "forecast_high_remaining": state.forecast_high_remaining,
        "forecast_low_remaining": state.forecast_low_remaining,
        "forecast_max_for_day": state.forecast_max_for_day,
        "forecast_min_for_day": state.forecast_min_for_day,
        "local_hour": local_dt.hour + local_dt.minute / 60.0,
        "day_of_year": local_dt.timetuple().tm_yday,
        "month": month,
        "season": season(month),
        "data_quality_score": state.data_quality_score,
        "data_age_minutes": state.data_age_minutes,
    }


def season(month: int) -> str:
    if month in {12, 1, 2}:
        return "winter"
    if month in {3, 4, 5}:
        return "spring"
    if month in {6, 7, 8}:
        return "summer"
    return "fall"
