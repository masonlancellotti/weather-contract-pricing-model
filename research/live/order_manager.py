from __future__ import annotations

from config import Settings, settings
from strategies.base import TradeSignal


class OrderManager:
    """Live execution placeholder. Real orders are intentionally not implemented."""

    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg

    def submit_live_order(self, signal: TradeSignal) -> None:
        self.cfg.require_live_trading_enabled()
        raise NotImplementedError("Live order entry is intentionally disabled until paper trader/backtester are validated.")
