from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from backtest.execution import NormalizedOrderBook
from backtest.fees import ConservativeFixedFeeModel, FeeModel
from data.storage import Storage
from research.edge_types import LIVE_PAPER
from research.opportunity_ranker import OpportunityRanker
from strategies.base import TradeSignal


@dataclass
class PaperTrader:
    storage: Storage = field(default_factory=Storage)
    fee_model: FeeModel = field(default_factory=ConservativeFixedFeeModel)

    def submit(self, signal: TradeSignal, orderbook: NormalizedOrderBook, quantity: float = 1.0) -> dict:
        status = "rejected"
        fill_price = None
        if signal.action == "BUY_YES" and orderbook.yes_ask is not None and orderbook.available_depth("buy_yes", orderbook.yes_ask) >= quantity:
            status = "filled"
            fill_price = orderbook.yes_ask
        elif signal.action == "BUY_NO" and orderbook.no_ask is not None and orderbook.available_depth("buy_no", orderbook.no_ask) >= quantity:
            status = "filled"
            fill_price = orderbook.no_ask
        payload = {
            "market_ticker": signal.market_ticker,
            "strategy": signal.strategy,
            "action": signal.action,
            "status": status,
            "fill_price_cents": fill_price,
            "quantity": quantity,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "reason": signal.reason,
        }
        self.storage.insert_json("paper_orders", payload, market_ticker=signal.market_ticker, order_time=datetime.now(timezone.utc), status=status)
        return payload

    def run_once(self, strategy: str = "rank_opportunities", weather_only: bool = True, mode: str = "taker_paper", max_markets: int = 100) -> dict[str, Any]:
        ranker = OpportunityRanker(storage=self.storage)
        ranked = ranker.rank(weather_only=weather_only, max_markets=max_markets, persist_exports=True)
        orders: list[dict[str, Any]] = []
        for row in ranked.rows:
            if row["recommended_action"] not in {"PAPER_SIGNAL", "TAKER_BUY_YES_CANDIDATE", "TAKER_BUY_NO_CANDIDATE", "PASSIVE_QUOTE_CANDIDATE"}:
                if row["recommended_action"] == "SKIP":
                    self._record_decision(row, strategy, mode, fill_status="rejected_skip")
                continue
            if mode == "passive_paper_conservative" and row["recommended_action"] != "PASSIVE_QUOTE_CANDIDATE":
                continue
            if mode == "taker_paper" and row["recommended_action"] == "PASSIVE_QUOTE_CANDIDATE":
                continue
            order = self._paper_order_from_candidate(row, strategy, mode)
            orders.append(order)
        return {
            "mode": mode,
            "strategy": strategy,
            "orders_logged": len(orders),
            "message": "Paper trading only. No real orders sent.",
            "orders": orders[:25],
        }

    def _paper_order_from_candidate(self, row: dict[str, Any], strategy: str, mode: str) -> dict[str, Any]:
        action = row["recommended_action"]
        side = "yes" if "YES" in action else "no" if "NO" in action else "passive"
        intended = row.get("yes_ask") if side == "yes" else row.get("no_ask") if side == "no" else row.get("yes_bid")
        fill_status = "filled_paper" if mode == "taker_paper" and intended is not None else "submitted_paper_no_fill"
        order = {
            "market_ticker": row["market_ticker"],
            "strategy": strategy,
            "edge_type": row.get("edge_type"),
            "execution_type": LIVE_PAPER,
            "action": action,
            "side": side,
            "intended_price": intended,
            "assumed_fill_price": intended if fill_status == "filled_paper" else None,
            "contracts": 1.0,
            "fair_yes_price": row.get("fair_yes_price"),
            "edge_cents": row.get("edge_after_buffers_cents"),
            "fill_status": fill_status,
            "reason": row.get("reason"),
            "raw_json": row.get("raw_json"),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self.storage.insert_json(
            "paper_orders",
            order,
            market_ticker=order["market_ticker"],
            order_time=datetime.now(timezone.utc),
            status=fill_status,
            strategy=strategy,
            edge_type=order["edge_type"],
            execution_type=order["execution_type"],
            action=order["action"],
            side=order["side"],
            intended_price=order["intended_price"],
            assumed_fill_price=order["assumed_fill_price"],
            contracts=order["contracts"],
            fair_yes_price=order["fair_yes_price"],
            edge_cents=order["edge_cents"],
            fill_status=fill_status,
            reason=order["reason"],
            raw_json=order["raw_json"],
        )
        if fill_status == "filled_paper":
            self.storage.insert_json(
                "paper_positions",
                order,
                market_ticker=order["market_ticker"],
                side=order["side"],
                quantity=1.0,
                contracts=1.0,
                avg_price_cents=order["assumed_fill_price"],
                current_mark=order["assumed_fill_price"],
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                settlement_status="open",
            )
        return order

    def _record_decision(self, row: dict[str, Any], strategy: str, mode: str, fill_status: str) -> None:
        payload = {
            "market_ticker": row.get("market_ticker"),
            "strategy": strategy,
            "execution_type": LIVE_PAPER,
            "paper_mode": mode,
            "action": row.get("recommended_action"),
            "fill_status": fill_status,
            "reason": row.get("reason"),
            "candidate": row,
        }
        self.storage.insert_json(
            "paper_orders",
            payload,
            market_ticker=row.get("market_ticker"),
            order_time=datetime.now(timezone.utc),
            status=fill_status,
            strategy=strategy,
            edge_type=row.get("edge_type"),
            execution_type=LIVE_PAPER,
            action=row.get("recommended_action"),
            fill_status=fill_status,
            reason=row.get("reason"),
        )
