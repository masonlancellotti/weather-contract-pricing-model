from __future__ import annotations

from dataclasses import dataclass

from config import Settings, settings
from strategies.base import TradeSignal


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    action: str
    reason: str


class RiskManager:
    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg

    def evaluate(self, signal: TradeSignal, features: dict, open_exposure_dollars: float = 0.0, market_exposure_dollars: float = 0.0) -> RiskDecision:
        if signal.action in {"SKIP", "WATCH"}:
            return RiskDecision(False, "SKIP", signal.skip_reason or signal.reason or "no actionable signal")
        if signal.edge_cents is None or signal.edge_cents < self.cfg.min_edge_cents:
            return RiskDecision(False, "SKIP", "edge below minimum")
        if features.get("parse_confidence", 0.0) < 0.75:
            return RiskDecision(False, "SKIP", "parse confidence below risk gate")
        if features.get("station_confidence", 0.0) < 0.75:
            return RiskDecision(False, "SKIP", "station confidence below risk gate")
        if features.get("data_age_minutes") is not None and features["data_age_minutes"] > self.cfg.max_weather_data_age_minutes:
            return RiskDecision(False, "SKIP", "weather data stale")
        if features.get("spread") is not None and features["spread"] < self.cfg.min_spread_cents:
            return RiskDecision(False, "SKIP", "spread too tight to overcome costs")
        if market_exposure_dollars >= self.cfg.max_market_exposure:
            return RiskDecision(False, "SKIP", "market exposure limit reached")
        if open_exposure_dollars >= self.cfg.max_total_exposure:
            return RiskDecision(False, "SKIP", "total exposure limit reached")
        return RiskDecision(True, "PAPER" if not self.cfg.enable_live_trading else "TRADE_CANDIDATE", "risk checks passed")
