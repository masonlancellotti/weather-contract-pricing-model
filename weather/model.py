"""
weather/model.py - Simple statistical temperature model.

Approach:
  1. Build a baseline from recent daily highs
  2. Blend with current hourly forecast
  3. Account for forecast error as uncertainty
  4. Output a probability distribution across contract buckets

The model is intentionally conservative. If data quality is poor, confidence
drops so downstream risk logic can suppress trades.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from models import ModelOutput, WeatherForecast, WeatherObs
from weather.mapping import CityMeta

UTC = timezone.utc

log = logging.getLogger("weather.model")


def _normal_pdf(x: float, mu: float, sigma: float) -> float:
    return math.exp(-0.5 * ((x - mu) / sigma) ** 2) / (sigma * math.sqrt(2 * math.pi))


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """Cumulative normal distribution using math.erf."""
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def prob_in_range(lo: Optional[float], hi: Optional[float], mu: float, sigma: float) -> float:
    """P(lo <= X < hi) for a normal distribution with given mu, sigma."""
    if lo is None and hi is None:
        return 1.0
    if lo is None:
        return _normal_cdf(hi, mu, sigma)
    if hi is None:
        return 1 - _normal_cdf(lo, mu, sigma)
    return _normal_cdf(hi, mu, sigma) - _normal_cdf(lo, mu, sigma)


def build_model(
    city_meta: CityMeta,
    forecast: Optional[WeatherForecast],
    obs: Optional[WeatherObs],
    historical_highs: list[dict],
    contract_buckets: list[dict],
    observed_high_so_far: Optional[float] = None,
    current_local_date: Optional[str] = None,
    current_local_time: Optional[datetime] = None,
) -> ModelOutput:
    """
    Produce a ModelOutput with probability per bucket.

    If data is insufficient, returns low-confidence output that should not
    trigger trades.
    """
    import pytz

    settlement_date = forecast.forecast_for if forecast else datetime.now(UTC).date().isoformat()
    local_tz = pytz.timezone(city_meta.timezone)
    current_local_time = current_local_time or datetime.now(UTC).astimezone(local_tz)
    if current_local_time.tzinfo is None:
        current_local_time = local_tz.localize(current_local_time)
    else:
        current_local_time = current_local_time.astimezone(local_tz)
    current_local_date = current_local_date or current_local_time.date().isoformat()
    is_same_day_contract = settlement_date == current_local_date

    if len(historical_highs) >= 14:
        recent_temps = [h["high_f"] for h in historical_highs[-30:]]
        hist_mean = _mean(recent_temps)
        hist_std = _std(recent_temps)
    else:
        log.warning(
            "%s: insufficient historical data (%d days) - low confidence",
            city_meta.city_code,
            len(historical_highs),
        )
        hist_mean = None
        hist_std = 10.0

    obs_temp = obs.temp_f if obs else None
    if observed_high_so_far is not None and obs_temp is not None:
        observed_high_so_far = max(observed_high_so_far, obs_temp)
    elif observed_high_so_far is None and obs_temp is not None:
        observed_high_so_far = obs_temp

    remaining_forecast_high = _remaining_forecast_high(forecast, settlement_date, current_local_time, local_tz)
    forecast_high = _forecast_anchor(
        forecast,
        hist_mean,
        is_same_day_contract,
        observed_high_so_far,
        remaining_forecast_high,
    )
    forecast_rmse = 4.0 if forecast else 8.0

    if forecast_high is not None and hist_mean is not None:
        expected_high = 0.85 * forecast_high + 0.15 * hist_mean
        expected_high = max(forecast_high - 4.0, min(forecast_high + 4.0, expected_high))
        combined_std = math.sqrt(forecast_rmse ** 2 + (0.15 * hist_std) ** 2)
    elif forecast_high is not None:
        expected_high = forecast_high
        combined_std = forecast_rmse
    elif hist_mean is not None:
        expected_high = hist_mean
        combined_std = max(hist_std, 6.0)
    else:
        log.warning("%s: no forecast or historical data - cannot model", city_meta.city_code)
        return _null_output(city_meta.city_code, settlement_date, contract_buckets)

    if obs:
        obs_local_time = obs.obs_time.astimezone(local_tz)
        hour_of_day = obs_local_time.hour
        if hour_of_day >= 14 and obs.temp_f > expected_high:
            expected_high = 0.5 * expected_high + 0.5 * obs.temp_f
            combined_std = max(combined_std * 0.7, 2.0)

    confidence = 1.0
    if len(historical_highs) < 14:
        confidence *= 0.5
    if not forecast:
        confidence *= 0.6
    if forecast and forecast.source == "open_meteo":
        confidence *= 0.85
    if is_same_day_contract and observed_high_so_far is None:
        confidence *= 0.7
    if is_same_day_contract and remaining_forecast_high is None:
        confidence *= 0.85
    confidence = max(0.1, min(1.0, confidence))

    bucket_probs = _compute_bucket_probs(contract_buckets, expected_high, combined_std)

    log.info(
        "%s model: expected_high=%.1fF std=%.1fF confidence=%.2f forecast=%s observed_hi=%s remaining_fc=%s",
        city_meta.city_code,
        expected_high,
        combined_std,
        confidence,
        f"{forecast_high:.1f}" if forecast_high is not None else "n/a",
        f"{observed_high_so_far:.1f}" if observed_high_so_far is not None else "n/a",
        f"{remaining_forecast_high:.1f}" if remaining_forecast_high is not None else "n/a",
    )

    return ModelOutput(
        city=city_meta.city_code,
        settlement_date=settlement_date,
        expected_high=expected_high,
        std_dev=combined_std,
        bucket_probs=bucket_probs,
        confidence=confidence,
        obs_temp=obs_temp,
        forecast_temp=forecast_high,
        observed_high_so_far=observed_high_so_far,
        remaining_forecast_high=remaining_forecast_high,
        local_hour=current_local_time.hour,
    )


def _forecast_anchor(
    forecast: Optional[WeatherForecast],
    hist_mean: Optional[float],
    is_same_day_contract: bool,
    observed_high_so_far: Optional[float],
    remaining_forecast_high: Optional[float],
) -> Optional[float]:
    if not forecast:
        return hist_mean
    forecast_high = forecast.high_f
    if is_same_day_contract and observed_high_so_far is not None:
        forecast_high = max(forecast_high, observed_high_so_far)
    if is_same_day_contract and remaining_forecast_high is not None:
        forecast_high = max(forecast_high, remaining_forecast_high)
    return forecast_high


def _remaining_forecast_high(
    forecast: Optional[WeatherForecast],
    settlement_date: str,
    current_local_time: datetime,
    local_tz,
) -> Optional[float]:
    if not forecast or not forecast.hourly:
        return None
    future_hourly: list[float] = []
    for hour in forecast.hourly:
        try:
            hour_local = datetime.fromisoformat(str(hour["hour"]))
        except Exception:
            continue
        if hour_local.tzinfo is None:
            hour_local = local_tz.localize(hour_local)
        else:
            hour_local = hour_local.astimezone(local_tz)
        if hour_local.date().isoformat() != settlement_date:
            continue
        if hour_local >= current_local_time:
            future_hourly.append(float(hour["temp_f"]))
    return max(future_hourly) if future_hourly else None


def _compute_bucket_probs(
    buckets: list[dict],
    mu: float,
    sigma: float,
) -> dict[str, float]:
    """Compute P(high in bucket) for each bucket using normal CDF."""
    probs = {}
    total = 0.0
    for bucket in buckets:
        lo = bucket.get("min")
        hi = bucket.get("max")
        probability = prob_in_range(lo, hi, mu, sigma)
        probs[bucket["label"]] = probability
        total += probability
    if total > 0 and abs(total - 1.0) > 0.01:
        log.debug("Bucket probs sum=%.4f - renormalizing", total)
        probs = {label: probability / total for label, probability in probs.items()}
    return probs


def _null_output(city: str, settlement_date: str, buckets: list[dict]) -> ModelOutput:
    """Return a zero-confidence output that should not trigger trades."""
    count = len(buckets) or 1
    uniform = 1.0 / count
    return ModelOutput(
        city=city,
        settlement_date=settlement_date,
        expected_high=0.0,
        std_dev=99.0,
        bucket_probs={bucket["label"]: uniform for bucket in buckets},
        confidence=0.0,
        obs_temp=None,
        forecast_temp=None,
        observed_high_so_far=None,
        remaining_forecast_high=None,
        local_hour=None,
    )


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mean = _mean(xs)
    return math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))


def snapshots_to_buckets(snapshots) -> list[dict]:
    """Convert MarketSnapshot list to bucket spec for model input."""
    buckets = []
    for snapshot in snapshots:
        if snapshot.bucket_label and (snapshot.bucket_min is not None or snapshot.bucket_max is not None):
            buckets.append(
                {
                    "label": snapshot.bucket_label,
                    "min": snapshot.bucket_min,
                    "max": snapshot.bucket_max,
                }
            )
    return buckets
