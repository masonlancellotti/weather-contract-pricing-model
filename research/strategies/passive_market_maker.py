from __future__ import annotations

from dataclasses import dataclass

from backtest.execution import floor_quote
from models.baseline_model import ModelPrediction
from parsing.weather_contract import WeatherContract
from strategies.base import TradeSignal


@dataclass(frozen=True)
class PassiveMarketMakerConfig:
    min_spread_cents: int = 8
    quote_edge_cents: int = 6
    fee_buffer_cents: int = 2
    max_order_age_seconds: int = 120
    adverse_selection_penalty_cents: int = 2


class PassiveMarketMakerStrategy:
    name = "passive_market_maker"

    def __init__(self, config: PassiveMarketMakerConfig | None = None):
        self.config = config or PassiveMarketMakerConfig()

    def quote(self, contract: WeatherContract, features: dict, prediction: ModelPrediction) -> list[TradeSignal]:
        if not contract.is_tradable:
            return [_skip(contract, contract.not_tradable_reason() or "contract not tradable")]
        spread = features.get("spread")
        if spread is None or spread < self.config.min_spread_cents:
            return [_skip(contract, "spread too tight for passive quoting")]
        if prediction.confidence < 0.75:
            return [_skip(contract, "fair-value confidence too low")]
        fair = prediction.fair_value_cents
        bid = floor_quote(fair - self.config.quote_edge_cents - self.config.fee_buffer_cents)
        ask = floor_quote(fair + self.config.quote_edge_cents + self.config.fee_buffer_cents)
        return [
            TradeSignal(contract.market_ticker, self.name, "BUY_YES", yes_fair_cents=fair, edge_cents=fair - bid, confidence=prediction.confidence, reason=f"Passive YES bid candidate at {bid}; fair {fair:.1f}."),
            TradeSignal(contract.market_ticker, self.name, "SELL_YES", yes_fair_cents=fair, edge_cents=ask - fair, confidence=prediction.confidence, reason=f"Passive YES ask candidate at {ask}; fair {fair:.1f}."),
        ]


def _skip(contract: WeatherContract, reason: str) -> TradeSignal:
    return TradeSignal(contract.market_ticker, "passive_market_maker", "SKIP", skip_reason=reason, reason=reason)
