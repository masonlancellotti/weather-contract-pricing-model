from __future__ import annotations

from dataclasses import dataclass

from backtest.fees import ConservativeFixedFeeModel, FeeModel
from parsing.weather_contract import WeatherContract
from strategies.base import TradeSignal


@dataclass(frozen=True)
class LadderConsistencyConfig:
    min_violation_cents: int = 2
    min_edge_after_fees: int = 3
    require_same_event_date_city: bool = True


class LadderConsistencyStrategy:
    name = "ladder_consistency"

    def __init__(self, config: LadderConsistencyConfig | None = None, fee_model: FeeModel | None = None):
        self.config = config or LadderConsistencyConfig()
        self.fee_model = fee_model or ConservativeFixedFeeModel()

    def generate_group(self, contracts: list[WeatherContract], features_by_ticker: dict[str, dict]) -> list[TradeSignal]:
        high_contracts = [
            c
            for c in contracts
            if c.variable_type == "high_temp" and c.threshold is not None and c.comparator in {"gte", "gt"}
        ]
        high_contracts.sort(key=lambda c: c.threshold or 0)
        signals: list[TradeSignal] = []
        for lower, higher in zip(high_contracts, high_contracts[1:], strict=False):
            if self.config.require_same_event_date_city and not _same_ladder_group(lower, higher):
                continue
            lower_features = features_by_ticker.get(lower.market_ticker, {})
            higher_features = features_by_ticker.get(higher.market_ticker, {})
            lower_yes_ask = lower_features.get("yes_ask")
            higher_yes_bid = higher_features.get("yes_bid")
            if lower_yes_ask is None or higher_yes_bid is None:
                continue
            gross_violation = higher_yes_bid - lower_yes_ask
            fee_cents = self.fee_model.fee_cents(lower_yes_ask) + self.fee_model.fee_cents(higher_yes_bid)
            net_edge = gross_violation - fee_cents
            if gross_violation >= self.config.min_violation_cents and net_edge >= self.config.min_edge_after_fees:
                reason = (
                    f"Ladder violation: lower threshold {lower.threshold:g} ask {lower_yes_ask} is below "
                    f"higher threshold {higher.threshold:g} bid {higher_yes_bid}; net edge after conservative fees {net_edge:.1f}c."
                )
                signals.append(
                    TradeSignal(
                        market_ticker=lower.market_ticker,
                        paired_market_ticker=higher.market_ticker,
                        strategy=self.name,
                        action="BUY_YES",
                        edge_cents=net_edge,
                        confidence=min(lower.parse_confidence, higher.parse_confidence),
                        reason=reason,
                    )
                )
                signals.append(
                    TradeSignal(
                        market_ticker=higher.market_ticker,
                        paired_market_ticker=lower.market_ticker,
                        strategy=self.name,
                        action="SELL_YES",
                        edge_cents=net_edge,
                        confidence=min(lower.parse_confidence, higher.parse_confidence),
                        reason=reason,
                    )
                )
        return signals


def _same_ladder_group(a: WeatherContract, b: WeatherContract) -> bool:
    return a.event_ticker == b.event_ticker and a.city == b.city and a.local_date == b.local_date
