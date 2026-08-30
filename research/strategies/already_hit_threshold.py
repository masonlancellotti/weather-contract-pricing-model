from __future__ import annotations

from dataclasses import dataclass

from models.baseline_model import ModelPrediction
from parsing.weather_contract import WeatherContract
from strategies.base import TradeSignal


@dataclass(frozen=True)
class AlreadyHitThresholdConfig:
    min_yes_discount_cents: int = 3
    required_parse_confidence: float = 0.9
    required_station_confidence: float = 0.9


class AlreadyHitThresholdStrategy:
    name = "already_hit_threshold"

    def __init__(self, config: AlreadyHitThresholdConfig | None = None):
        self.config = config or AlreadyHitThresholdConfig()

    def generate(self, contract: WeatherContract, features: dict, prediction: ModelPrediction) -> TradeSignal:
        if contract.parse_confidence < self.config.required_parse_confidence:
            return _skip(contract, "parse confidence below already-hit threshold")
        if features.get("station_confidence", 0.0) < self.config.required_station_confidence:
            return _skip(contract, "station confidence below already-hit threshold")
        if not features.get("is_threshold_already_hit"):
            return _skip(contract, "threshold not already hit")
        yes_ask = features.get("yes_ask")
        if yes_ask is None:
            return _skip(contract, "missing yes ask")
        fair = min(99.5, prediction.fair_value_cents)
        edge = fair - yes_ask
        if yes_ask <= 100 - self.config.min_yes_discount_cents and edge >= self.config.min_yes_discount_cents:
            return TradeSignal(
                market_ticker=contract.market_ticker,
                strategy=self.name,
                action="BUY_YES",
                yes_fair_cents=fair,
                edge_cents=edge,
                confidence=prediction.confidence,
                reason=f"Threshold already hit; yes ask {yes_ask} vs conservative fair {fair:.1f}. Verify official station/rules before any live order.",
            )
        return _skip(contract, f"already hit but ask {yes_ask} leaves insufficient edge vs fair {fair:.1f}")


def _skip(contract: WeatherContract, reason: str) -> TradeSignal:
    return TradeSignal(market_ticker=contract.market_ticker, strategy="already_hit_threshold", action="SKIP", skip_reason=reason, reason=reason)
