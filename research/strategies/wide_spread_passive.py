from __future__ import annotations

from dataclasses import dataclass

from models.weather_fair_value import FairValueResult


@dataclass(frozen=True)
class WideSpreadPassiveConfig:
    min_spread_cents: int = 8
    quote_edge_cents: int = 6
    fee_buffer_cents: int = 2
    adverse_selection_penalty_cents: int = 2


class WideSpreadPassiveStrategy:
    """Research-only passive liquidity strategy.

    This class proposes quotes around fair value. It does not claim fills and it
    does not place orders. Fill realism belongs in `backtest.passive_fill_model`.
    """

    name = "wide_spread_passive"

    def __init__(self, config: WideSpreadPassiveConfig | None = None):
        self.config = config or WideSpreadPassiveConfig()

    def quote_candidates(self, market_features: dict, fair: FairValueResult) -> list[dict]:
        spread = market_features.get("spread") or market_features.get("spread_cents")
        if spread is None or spread < self.config.min_spread_cents:
            return []
        if fair.confidence < 0.55:
            return []
        bid_yes = max(1.0, fair.fair_yes_price_cents - self.config.quote_edge_cents - self.config.fee_buffer_cents)
        bid_no = max(1.0, fair.fair_no_price_cents - self.config.quote_edge_cents - self.config.fee_buffer_cents)
        return [
            {"side": "BUY_YES", "limit_price": bid_yes, "edge_type": "PASSIVE_LIQUIDITY_SPREAD_EDGE", "reason": "Research-only wide-spread YES bid candidate."},
            {"side": "BUY_NO", "limit_price": bid_no, "edge_type": "PASSIVE_LIQUIDITY_SPREAD_EDGE", "reason": "Research-only wide-spread NO bid candidate."},
        ]
