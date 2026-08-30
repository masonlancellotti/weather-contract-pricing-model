from __future__ import annotations

from dataclasses import dataclass, field

from backtest.execution import NormalizedOrderBook
from backtest.fees import ConservativeFixedFeeModel, FeeModel
from strategies.base import TradeSignal


@dataclass
class ConservativeTakerSimulator:
    fee_model: FeeModel = field(default_factory=ConservativeFixedFeeModel)
    worse_fill_cents: int = 0

    def execute_signal(self, signal: TradeSignal, orderbook: NormalizedOrderBook, settlement_result: str, quantity: float = 1.0) -> dict | None:
        if signal.action == "BUY_YES":
            entry = orderbook.yes_ask
            payout = 100 if settlement_result == "yes" else 0
            side = "buy_yes"
        elif signal.action == "BUY_NO":
            entry = orderbook.no_ask
            payout = 100 if settlement_result == "no" else 0
            side = "buy_no"
        else:
            return None
        if entry is None:
            return None
        entry = min(99, entry + self.worse_fill_cents)
        if orderbook.available_depth(side, entry) < quantity:
            return None
        fee = self.fee_model.fee_cents(entry, quantity)
        gross = (payout - entry) * quantity
        net = gross - fee
        return {
            "market_ticker": signal.market_ticker,
            "strategy": signal.strategy,
            "action": signal.action,
            "entry_price_cents": entry,
            "quantity": quantity,
            "settlement_result": settlement_result,
            "gross_pnl_cents": gross,
            "fees_cents": fee,
            "net_pnl_cents": net,
            "edge_cents": signal.edge_cents or 0.0,
            "reason": signal.reason,
        }


@dataclass
class PassiveMakerSimulator:
    fee_model: FeeModel = field(default_factory=ConservativeFixedFeeModel)
    adverse_selection_penalty_cents: int = 2
    touched_fill_probability: float = 0.25

    def fill_if_traded_through(self, quote_price: int, later_trade_price: int, side: str) -> bool:
        if side == "buy_yes":
            return later_trade_price <= quote_price - self.adverse_selection_penalty_cents
        if side == "sell_yes":
            return later_trade_price >= quote_price + self.adverse_selection_penalty_cents
        return False
