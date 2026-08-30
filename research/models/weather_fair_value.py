from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from parsing.weather_contract import WeatherContract


@dataclass(frozen=True)
class FairValueResult:
    market_ticker: str
    fair_yes_probability: float
    fair_yes_price_cents: float
    confidence: float
    explanation: str
    uncertainty_cents: float
    no_trade_reason: str | None = None

    @property
    def fair_no_price_cents(self) -> float:
        return 100.0 - self.fair_yes_price_cents

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_ticker": self.market_ticker,
            "fair_yes_probability": self.fair_yes_probability,
            "fair_yes_price_cents": self.fair_yes_price_cents,
            "fair_no_price_cents": self.fair_no_price_cents,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "uncertainty_cents": self.uncertainty_cents,
            "no_trade_reason": self.no_trade_reason,
        }


class WeatherFairValueModel:
    version = "weather_fair_value_v0.1"

    def estimate(self, contract: WeatherContract, features: dict[str, Any]) -> FairValueResult:
        if contract.variable_type not in {"high_temp", "low_temp"}:
            return _no_trade(contract, "Unsupported variable type for fair-value model.")
        if contract.contract_type == "unknown":
            return _no_trade(contract, "Unknown contract type.")
        quality = _num(features.get("weather_asof_quality_score")) or _num(features.get("data_quality_score")) or 0.5
        station_conf = float(contract.station_confidence or 0.0)
        parse_conf = float(contract.parse_confidence or 0.0)
        confidence = max(0.0, min(0.95, quality, parse_conf or 0.5, station_conf or 0.75))
        current = _num(features.get("current_temp_asof") or features.get("current_temp"))
        max_so_far = _num(features.get("max_temp_so_far_asof") or features.get("max_temp_so_far"))
        min_so_far = _num(features.get("min_temp_so_far_asof") or features.get("min_temp_so_far"))
        forecast_high = _num(features.get("forecast_high_remaining_f") or features.get("forecast_high_remaining") or features.get("forecast_max_for_day"))
        forecast_low = _num(features.get("forecast_low_remaining_f") or features.get("forecast_low_remaining") or features.get("forecast_min_for_day"))
        trend_1h = _num(features.get("temp_trend_1h")) or 0.0
        local_hour = _num(features.get("local_hour")) or 12.0
        sigma = _default_sigma(local_hour, confidence)
        if contract.variable_type == "high_temp":
            center = _high_center(current, max_so_far, forecast_high, trend_1h, local_hour)
            p = _probability_for_contract(contract, center, sigma, high_or_low="high")
            explanation = f"High-temp distribution center={center:.1f}F sigma={sigma:.1f}F; max_so_far={max_so_far}; forecast_high={forecast_high}."
        else:
            center = _low_center(current, min_so_far, forecast_low, trend_1h, local_hour)
            p = _probability_for_contract(contract, center, sigma, high_or_low="low")
            explanation = f"Low-temp distribution center={center:.1f}F sigma={sigma:.1f}F; min_so_far={min_so_far}; forecast_low={forecast_low}."
        p = _clip_probability(_apply_guarantee_bounds(contract, p, max_so_far, min_so_far))
        uncertainty = max(4.0, min(25.0, sigma * 5.0 + (1.0 - confidence) * 12.0))
        no_trade = None
        if confidence < 0.55:
            no_trade = "Fair-value confidence below medium."
        return FairValueResult(contract.market_ticker, p, p * 100.0, confidence, explanation, uncertainty, no_trade)


def _probability_for_contract(contract: WeatherContract, center: float, sigma: float, high_or_low: str) -> float:
    if contract.contract_type == "range_bucket":
        if contract.range_low is None or contract.range_high is None:
            return 0.5
        return _normal_cdf((contract.range_high - center) / sigma) - _normal_cdf((contract.range_low - center) / sigma)
    if contract.threshold is None:
        return 0.5
    z = (contract.threshold - center) / sigma
    if contract.contract_type == "threshold_above":
        p = 1.0 - _normal_cdf(z)
        if contract.comparator == "gt":
            p = 1.0 - _normal_cdf(((contract.threshold + 0.5) - center) / sigma)
        return p
    if contract.contract_type == "threshold_below":
        p = _normal_cdf(z)
        if contract.comparator == "lt":
            p = _normal_cdf(((contract.threshold - 0.5) - center) / sigma)
        return p
    return 0.5


def _apply_guarantee_bounds(contract: WeatherContract, p: float, max_so_far: float | None, min_so_far: float | None) -> float:
    if contract.contract_type == "range_bucket":
        if contract.variable_type == "high_temp" and max_so_far is not None and contract.range_high is not None and max_so_far > contract.range_high:
            return 0.005
        if contract.variable_type == "low_temp" and min_so_far is not None and contract.range_low is not None and min_so_far < contract.range_low:
            return 0.005
        return p
    if contract.threshold is None:
        return p
    if contract.variable_type == "high_temp" and max_so_far is not None:
        if contract.contract_type == "threshold_above" and ((contract.comparator == "gt" and max_so_far > contract.threshold) or (contract.comparator != "gt" and max_so_far >= contract.threshold)):
            return 0.995
        if contract.contract_type == "threshold_below" and ((contract.comparator == "lt" and max_so_far >= contract.threshold) or (contract.comparator != "lt" and max_so_far > contract.threshold)):
            return 0.005
    if contract.variable_type == "low_temp" and min_so_far is not None:
        if contract.contract_type == "threshold_below" and ((contract.comparator == "lt" and min_so_far < contract.threshold) or (contract.comparator != "lt" and min_so_far <= contract.threshold)):
            return 0.995
        if contract.contract_type == "threshold_above" and ((contract.comparator == "gt" and min_so_far <= contract.threshold) or (contract.comparator != "gt" and min_so_far < contract.threshold)):
            return 0.005
    return p


def _high_center(current: float | None, max_so_far: float | None, forecast_high: float | None, trend_1h: float, local_hour: float) -> float:
    candidates = [v for v in [current, max_so_far, forecast_high] if v is not None]
    if not candidates:
        return 70.0
    center = max(candidates)
    if forecast_high is None and local_hour < 15:
        center += max(0.0, 4.0 - max(local_hour - 11.0, 0.0)) + max(trend_1h, 0.0)
    return center


def _low_center(current: float | None, min_so_far: float | None, forecast_low: float | None, trend_1h: float, local_hour: float) -> float:
    candidates = [v for v in [current, min_so_far, forecast_low] if v is not None]
    if not candidates:
        return 50.0
    center = min(candidates)
    if forecast_low is None and (local_hour >= 18 or local_hour <= 8):
        center -= max(-trend_1h, 0.0)
    return center


def _default_sigma(local_hour: float, confidence: float) -> float:
    base = 4.5 if local_hour < 12 else 3.5 if local_hour < 15 else 2.5
    return max(1.25, base + (1.0 - confidence) * 2.0)


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _clip_probability(value: float) -> float:
    return max(0.005, min(0.995, value))


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _no_trade(contract: WeatherContract, reason: str) -> FairValueResult:
    return FairValueResult(contract.market_ticker, 0.5, 50.0, 0.0, reason, 25.0, reason)
