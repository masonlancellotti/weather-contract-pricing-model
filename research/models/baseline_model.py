from __future__ import annotations

import math
from dataclasses import dataclass

from parsing.weather_contract import WeatherContract


@dataclass(frozen=True)
class ModelPrediction:
    market_ticker: str
    model_version: str
    yes_probability: float
    fair_value_cents: float
    confidence: float
    explanation: str
    warnings: list[str]

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class HeuristicWeatherModel:
    version = "heuristic_weather_v0.1"

    def predict(self, contract: WeatherContract, features: dict) -> ModelPrediction:
        warnings: list[str] = []
        if contract.threshold is None or contract.variable_type not in {"high_temp", "low_temp"}:
            return ModelPrediction(contract.market_ticker, self.version, 0.5, 50.0, 0.0, "Unsupported or unparsed contract.", ["unsupported contract"])
        if features.get("data_quality_score", 0.0) < 0.5:
            warnings.append("low weather data quality")
        if contract.variable_type == "high_temp":
            p, reason = self._daily_high_probability(contract, features)
        else:
            p, reason = self._daily_low_probability(contract, features)
        confidence = min(contract.parse_confidence, features.get("data_quality_score", 0.5), 0.95)
        if warnings:
            confidence = min(confidence, 0.55)
        p = max(0.005, min(0.995, p))
        return ModelPrediction(
            market_ticker=contract.market_ticker,
            model_version=self.version,
            yes_probability=p,
            fair_value_cents=p * 100.0,
            confidence=confidence,
            explanation=reason,
            warnings=warnings,
        )

    def _daily_high_probability(self, contract: WeatherContract, features: dict) -> tuple[float, str]:
        threshold = float(contract.threshold)
        max_so_far = features.get("max_temp_so_far")
        forecast_remaining = features.get("forecast_high_remaining") or features.get("forecast_max_for_day")
        trend = features.get("temp_trend_1h") or 0.0
        local_hour = features.get("local_hour") or 12.0
        if max_so_far is not None and max_so_far >= threshold:
            return 0.995, f"threshold already hit: max so far {max_so_far:.1f} >= {threshold:.1f}"
        best_remaining = max(v for v in [max_so_far, forecast_remaining] if v is not None) if any(v is not None for v in [max_so_far, forecast_remaining]) else threshold
        gap = best_remaining - threshold
        time_decay = max(0.35, 1.25 - max(local_hour - 12.0, 0) * 0.12)
        trend_adj = max(min(trend, 3.0), -3.0) * 0.25
        logit = (gap + trend_adj) / max(time_decay, 0.25)
        p = _sigmoid(logit)
        if local_hour >= 15 and forecast_remaining is not None and forecast_remaining < threshold - 1:
            p *= 0.35
        return p, f"high-temp heuristic: best remaining {best_remaining:.1f}, gap {gap:.1f}, local hour {local_hour:.1f}, trend {trend:.1f}"

    def _daily_low_probability(self, contract: WeatherContract, features: dict) -> tuple[float, str]:
        threshold = float(contract.threshold)
        min_so_far = features.get("min_temp_so_far")
        forecast_remaining = features.get("forecast_low_remaining") or features.get("forecast_min_for_day")
        trend = features.get("temp_trend_1h") or 0.0
        local_hour = features.get("local_hour") or 12.0
        if min_so_far is not None and min_so_far <= threshold:
            return 0.995, f"threshold already hit: min so far {min_so_far:.1f} <= {threshold:.1f}"
        best_remaining = min(v for v in [min_so_far, forecast_remaining] if v is not None) if any(v is not None for v in [min_so_far, forecast_remaining]) else threshold
        gap = threshold - best_remaining
        trend_adj = -max(min(trend, 3.0), -3.0) * 0.2
        time_factor = 1.0 if local_hour >= 18 or local_hour <= 8 else 0.7
        p = _sigmoid((gap + trend_adj) * time_factor)
        return p, f"low-temp heuristic: best remaining {best_remaining:.1f}, cold gap {gap:.1f}, local hour {local_hour:.1f}, trend {trend:.1f}"


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(min(x, 20), -20)))
