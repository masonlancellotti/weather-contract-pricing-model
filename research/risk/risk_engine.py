from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from config import Settings, settings


class RiskDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT_EDGE_TOO_SMALL = "REJECT_EDGE_TOO_SMALL"
    REJECT_DATA_STALE = "REJECT_DATA_STALE"
    REJECT_LOW_SETTLEMENT_CONFIDENCE = "REJECT_LOW_SETTLEMENT_CONFIDENCE"
    REJECT_TOO_MUCH_EXPOSURE = "REJECT_TOO_MUCH_EXPOSURE"
    REJECT_CONTRACT_UNKNOWN = "REJECT_CONTRACT_UNKNOWN"
    REJECT_LIQUIDITY_BAD = "REJECT_LIQUIDITY_BAD"
    REJECT_CORRELATED_EXPOSURE = "REJECT_CORRELATED_EXPOSURE"
    REJECT_DAILY_LOSS_LIMIT = "REJECT_DAILY_LOSS_LIMIT"


@dataclass(frozen=True)
class RiskResult:
    decision: RiskDecision
    reason: str

    @property
    def approved(self) -> bool:
        return self.decision == RiskDecision.APPROVE

    def to_dict(self) -> dict:
        return {"decision": self.decision.value, "reason": self.reason, "approved": self.approved}


class RiskEngine:
    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg

    def evaluate_candidate(self, candidate: dict[str, Any], current_exposure: dict[str, float] | None = None) -> RiskResult:
        current_exposure = current_exposure or {}
        if candidate.get("contract_type") in {None, "", "unknown"}:
            return RiskResult(RiskDecision.REJECT_CONTRACT_UNKNOWN, "Unknown contract type.")
        if float(candidate.get("settlement_quality_score") or candidate.get("settlement_confidence") or 0.0) < self.cfg.min_settlement_confidence:
            return RiskResult(RiskDecision.REJECT_LOW_SETTLEMENT_CONFIDENCE, "Settlement/source confidence below threshold.")
        if float(candidate.get("weather_data_age_minutes") or 0.0) > self.cfg.max_weather_data_age_minutes:
            return RiskResult(RiskDecision.REJECT_DATA_STALE, "Weather observation data is stale.")
        if float(candidate.get("forecast_data_age_minutes") or 0.0) > self.cfg.max_forecast_data_age_minutes:
            return RiskResult(RiskDecision.REJECT_DATA_STALE, "Forecast data is stale.")
        if float(candidate.get("edge_after_buffers_cents") or 0.0) < self.cfg.min_edge_after_buffers_cents:
            return RiskResult(RiskDecision.REJECT_EDGE_TOO_SMALL, "Edge after fees/buffers below minimum.")
        if float(candidate.get("depth") or 0.0) < self.cfg.passive_min_displayed_depth:
            return RiskResult(RiskDecision.REJECT_LIQUIDITY_BAD, "Displayed depth too thin.")
        market = str(candidate.get("market_ticker") or "")
        intended_notional = float(candidate.get("contracts") or 1.0) * float(candidate.get("intended_price") or candidate.get("entry_price") or 100.0) / 100.0
        if current_exposure.get(market, 0.0) + intended_notional > self.cfg.max_paper_dollars_per_market:
            return RiskResult(RiskDecision.REJECT_TOO_MUCH_EXPOSURE, "Market exposure would exceed paper limit.")
        if sum(current_exposure.values()) + intended_notional > self.cfg.max_paper_total_exposure:
            return RiskResult(RiskDecision.REJECT_TOO_MUCH_EXPOSURE, "Total paper exposure would exceed limit.")
        return RiskResult(RiskDecision.APPROVE, "Approved for paper/research mode only.")
