from __future__ import annotations

from dataclasses import dataclass

from models.baseline_model import ModelPrediction
from parsing.weather_contract import WeatherContract
from strategies.base import TradeSignal


@dataclass(frozen=True)
class LateDayLowFadeConfig:
    min_local_hour: float = 9.0
    min_threshold_gap: float = 2.0
    min_edge_cents: int = 7
    max_yes_fair_for_no_trade: int = 35
    min_liquidity_contracts: float = 10.0


class LateDayLowFadeStrategy:
    name = "late_day_low_fade"

    def __init__(self, config: LateDayLowFadeConfig | None = None):
        self.config = config or LateDayLowFadeConfig()

    def generate(self, contract: WeatherContract, features: dict, prediction: ModelPrediction) -> TradeSignal:
        if contract.variable_type != "low_temp":
            return _skip(contract, "not a low-temperature contract")
        if not contract.is_tradable or features.get("is_threshold_already_hit"):
            return _skip(contract, "not tradable or threshold already hit")
        local_hour = features.get("local_hour")
        gap_min = None if features.get("min_temp_so_far") is None or contract.threshold is None else features["min_temp_so_far"] - contract.threshold
        if local_hour is None or local_hour < self.config.min_local_hour:
            return _skip(contract, "too early for low fade")
        if gap_min is None or gap_min < self.config.min_threshold_gap:
            return _skip(contract, "low-so-far is too close to threshold")
        if features.get("liquidity_score", 0.0) < self.config.min_liquidity_contracts:
            return _skip(contract, "insufficient top-of-book liquidity")
        no_ask = features.get("no_ask")
        if no_ask is None:
            return _skip(contract, "missing no ask")
        yes_fair = prediction.fair_value_cents
        edge = 100.0 - yes_fair - no_ask
        if yes_fair > self.config.max_yes_fair_for_no_trade or edge < self.config.min_edge_cents:
            return _skip(contract, f"NO edge {edge:.1f} below threshold")
        return TradeSignal(
            market_ticker=contract.market_ticker,
            strategy=self.name,
            action="BUY_NO",
            yes_fair_cents=yes_fair,
            edge_cents=edge,
            confidence=prediction.confidence,
            reason=f"BUY NO candidate: low not hit after local hour {local_hour:.1f}; low-so-far gap {gap_min:.1f}F; edge {edge:.1f}c.",
        )


def _skip(contract: WeatherContract, reason: str) -> TradeSignal:
    return TradeSignal(market_ticker=contract.market_ticker, strategy="late_day_low_fade", action="SKIP", skip_reason=reason, reason=reason)
