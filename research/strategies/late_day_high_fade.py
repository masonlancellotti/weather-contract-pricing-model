from __future__ import annotations

from dataclasses import dataclass

from models.baseline_model import ModelPrediction
from parsing.weather_contract import WeatherContract
from strategies.base import TradeSignal


@dataclass(frozen=True)
class LateDayHighFadeConfig:
    min_local_hour: float = 14.0
    min_threshold_gap: float = 2.0
    max_forecast_remaining_gap: float = -0.5
    min_edge_cents: int = 7
    max_yes_fair_for_no_trade: int = 35
    min_liquidity_contracts: float = 10.0


class LateDayHighFadeStrategy:
    name = "late_day_high_fade"

    def __init__(self, config: LateDayHighFadeConfig | None = None):
        self.config = config or LateDayHighFadeConfig()

    def generate(self, contract: WeatherContract, features: dict, prediction: ModelPrediction) -> TradeSignal:
        if contract.variable_type != "high_temp":
            return _skip(contract, "not a high-temperature contract")
        if not contract.is_tradable:
            return _skip(contract, contract.not_tradable_reason() or "contract not tradable")
        if features.get("is_threshold_already_hit"):
            return _skip(contract, "threshold already hit; fade invalid")
        local_hour = features.get("local_hour")
        if local_hour is None or local_hour < self.config.min_local_hour:
            return _skip(contract, "too early for late-day fade")
        gap_max = features.get("threshold_gap_max_so_far")
        if gap_max is None or gap_max < self.config.min_threshold_gap:
            return _skip(contract, "max-so-far is too close to threshold")
        forecast_gap = features.get("threshold_gap_forecast_high")
        if forecast_gap is None or forecast_gap <= abs(self.config.max_forecast_remaining_gap):
            return _skip(contract, "remaining forecast is too close or unavailable")
        trend = features.get("temp_trend_1h") or 0.0
        if trend > 1.0:
            return _skip(contract, "temperature trend is still rising")
        if features.get("liquidity_score", 0.0) < self.config.min_liquidity_contracts:
            return _skip(contract, "insufficient top-of-book liquidity")
        no_ask = features.get("no_ask")
        if no_ask is None:
            return _skip(contract, "missing no ask")
        yes_fair = prediction.fair_value_cents
        if yes_fair > self.config.max_yes_fair_for_no_trade:
            return _skip(contract, f"model yes fair {yes_fair:.1f} too high for NO trade")
        no_fair = 100.0 - yes_fair
        edge = no_fair - no_ask
        if edge < self.config.min_edge_cents:
            return _skip(contract, f"NO edge {edge:.1f} below {self.config.min_edge_cents}c")
        return TradeSignal(
            market_ticker=contract.market_ticker,
            strategy=self.name,
            action="BUY_NO",
            yes_fair_cents=yes_fair,
            edge_cents=edge,
            confidence=prediction.confidence,
            reason=(
                f"BUY NO candidate: {contract.city} high {contract.comparator} {contract.threshold:g}F. "
                f"Local hour {local_hour:.1f}. Max so far gap {gap_max:.1f}F, forecast gap {forecast_gap:.1f}F, "
                f"trend {trend:.1f}F/hr. YES fair {yes_fair:.1f}, NO ask {no_ask}, edge {edge:.1f}c."
            ),
            risk_notes=list(prediction.warnings),
        )


def _skip(contract: WeatherContract, reason: str) -> TradeSignal:
    return TradeSignal(market_ticker=contract.market_ticker, strategy="late_day_high_fade", action="SKIP", skip_reason=reason, reason=reason)
