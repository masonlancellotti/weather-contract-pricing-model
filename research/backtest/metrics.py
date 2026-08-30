from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BacktestMetrics:
    gross_pnl_cents: float
    net_pnl_cents: float
    trades: int
    win_rate: float
    average_edge_cents: float
    max_drawdown_cents: float
    largest_loss_cents: float

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def compute_metrics(trades: list[dict]) -> BacktestMetrics:
    if not trades:
        return BacktestMetrics(0.0, 0.0, 0, 0.0, 0.0, 0.0, 0.0)
    gross = sum(float(t.get("gross_pnl_cents", 0.0)) for t in trades)
    net = sum(float(t.get("net_pnl_cents", 0.0)) for t in trades)
    wins = sum(1 for t in trades if float(t.get("net_pnl_cents", 0.0)) > 0)
    avg_edge = sum(float(t.get("edge_cents", 0.0)) for t in trades) / len(trades)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    largest_loss = 0.0
    for trade in trades:
        pnl = float(trade.get("net_pnl_cents", 0.0))
        equity += pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        largest_loss = min(largest_loss, pnl)
    return BacktestMetrics(
        gross_pnl_cents=gross,
        net_pnl_cents=net,
        trades=len(trades),
        win_rate=wins / len(trades),
        average_edge_cents=avg_edge,
        max_drawdown_cents=max_dd,
        largest_loss_cents=largest_loss,
    )


def brutally_honest_summary(metrics: BacktestMetrics) -> str:
    if metrics.trades < 30 or metrics.net_pnl_cents <= 0:
        return "No robust edge found under conservative assumptions."
    return (
        f"Promising but not proven: trades={metrics.trades}, net P&L={metrics.net_pnl_cents / 100:.2f}, "
        f"max drawdown={metrics.max_drawdown_cents / 100:.2f}, win rate={metrics.win_rate:.1%}."
    )
