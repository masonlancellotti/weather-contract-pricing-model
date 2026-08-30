from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from data.storage import Storage
from data.weather_settlement_loader import SETTLEMENT_VERSION
from parsing.weather_contract import PARSER_VERSION
from research.liquidity_analysis import LiquidityAnalyzer
from research.signal_validation import SignalValidator


@dataclass(frozen=True)
class TradingReadinessResult:
    status: str
    message: str
    reasons: list[str]
    metrics: dict[str, Any]
    next_command: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "reasons": self.reasons,
            "metrics": self.metrics,
            "next_command": self.next_command,
        }

    def to_text(self) -> str:
        lines = [f"Trading readiness: {self.status}", self.message, "Reasons:"]
        lines.extend(f"- {reason}" for reason in self.reasons)
        lines.append(f"Next command: {self.next_command}")
        return "\n".join(lines)


class TradingReadiness:
    def __init__(self, storage: Storage | None = None):
        self.storage = storage or Storage()

    def evaluate(self, last_days: int = 7) -> TradingReadinessResult:
        since = (date.today() - timedelta(days=max(last_days, 1))).isoformat()
        metrics = {
            "orderbook_snapshots": self._count_since("orderbook_snapshots_live", "ts", since),
            "weather_observation_rows": self._count_since("weather_observation_snapshots_live", "ts_recorded", since),
            "weather_forecast_rows": self._count_since("weather_forecast_snapshots_live", "ts_recorded", since),
            "replay_rows": self._count_since("recorded_orderbook_replay_snapshots", "ts", since),
            "settlement_labels_primary": self._primary_settlement_count(),
            "stale_runs": self._stale_count(),
            "parser_version": PARSER_VERSION,
            "settlement_version": SETTLEMENT_VERSION,
        }
        sweeps = self._latest_sweeps(since)
        validation = SignalValidator(self.storage).validate(last_days=last_days).summary_by_strategy
        liquidity = LiquidityAnalyzer(self.storage).analyze(last_days=last_days, persist_exports=False).summary
        metrics["latest_sweeps"] = sweeps[:10]
        metrics["signal_validation"] = validation
        metrics["liquidity"] = liquidity
        reasons: list[str] = []
        if metrics["orderbook_snapshots"] < 1000:
            reasons.append("Not enough recorded orderbook snapshots.")
        if metrics["weather_forecast_rows"] == 0:
            reasons.append("No recorded forecast snapshots; late-day forecast-aware testing is weak.")
        if metrics["replay_rows"] == 0:
            reasons.append("No recorded replay rows. Build replay after settlements exist.")
        if metrics["settlement_labels_primary"] == 0:
            reasons.append("No primary high-confidence settlement labels.")
        if metrics["stale_runs"] > 0:
            reasons.append("Stale runs exist; dashboard/report must exclude them.")
        best = sweeps[0] if sweeps else None
        if not best:
            reasons.append("No clean recorded strategy sweep results.")
        elif float(best.get("net_pnl") or 0.0) <= 0:
            reasons.append("Best clean sweep does not have positive net P&L.")
        elif int(best.get("fills") or 0) < 30:
            reasons.append("Best clean sweep has fewer than 30 fills.")
        if liquidity.get("passive_verdict") not in {"PAPER_READY_SPECIFIC_STRATEGY"}:
            reasons.append("Passive liquidity has not shown reliable fill/anti-adverse-selection evidence.")
        if validation and not any(item.get("recommendation") == "interesting_signal" for item in validation):
            reasons.append("Signals have not clearly beaten future prices.")
        status = "NOT_READY_DATA_INCOMPLETE"
        next_command = "python main.py build-recorded-replay --last-days 7"
        if metrics["replay_rows"] > 0 and best is None:
            status = "NOT_READY_NO_EDGE"
            next_command = "python main.py sweep-recorded --last-days 7"
        elif best and float(best.get("net_pnl") or 0.0) <= 0:
            status = "NOT_READY_NO_EDGE"
            next_command = "python main.py analyze-liquidity --last-days 7"
        elif best and int(best.get("fills") or 0) < 30 and float(best.get("net_pnl") or 0.0) > 0:
            status = "RESEARCH_READY_MORE_DATA_NEEDED"
            next_command = "python main.py edge-report --last-days 7"
        if _paper_ready(best, validation, liquidity):
            status = "PAPER_READY_SPECIFIC_STRATEGY"
            next_command = "python main.py paper-trade --strategy " + str(best.get("strategy")) + " --weather-only"
        if _tiny_live_ready(best, validation, liquidity):
            status = "TINY_LIVE_READY_SPECIFIC_STRATEGY"
            next_command = "Do not run live trading from this repo yet; manual approval and paper results required."
        message = _message(status)
        if status == "TINY_LIVE_READY_SPECIFIC_STRATEGY":
            reasons.append("This status should be rare; verify paper/live gates manually before any real money.")
        return TradingReadinessResult(status, message, reasons, metrics, next_command)

    def _count_since(self, table: str, column: str, since: str) -> int:
        frame = self.storage.fetch_sql(f"SELECT COUNT(*) AS count FROM {table} WHERE date({column}) >= :since", {"since": since})
        return int(frame.iloc[0]["count"]) if not frame.empty else 0

    def _primary_settlement_count(self) -> int:
        frame = self.storage.fetch_sql("SELECT COUNT(*) AS count FROM settlement_labels WHERE confidence >= 0.85")
        return int(frame.iloc[0]["count"]) if not frame.empty else 0

    def _stale_count(self) -> int:
        frame = self.storage.fetch_sql("SELECT COUNT(*) AS count FROM backtest_runs WHERE COALESCE(is_stale, 0) = 1")
        return int(frame.iloc[0]["count"]) if not frame.empty else 0

    def _latest_sweeps(self, since: str) -> list[dict[str, Any]]:
        frame = self.storage.fetch_sql(
            """
            SELECT * FROM recorded_strategy_sweeps
            WHERE COALESCE(is_stale, 0) = 0
              AND COALESCE(parser_version, '') = :parser_version
              AND COALESCE(settlement_version, '') = :settlement_version
              AND date(ts) >= :since
            ORDER BY net_pnl DESC, fills DESC
            LIMIT 25
            """,
            {"parser_version": PARSER_VERSION, "settlement_version": SETTLEMENT_VERSION, "since": since},
        )
        return [] if frame.empty else frame.to_dict("records")


def _paper_ready(best: dict[str, Any] | None, validation: list[dict[str, Any]], liquidity: dict[str, Any]) -> bool:
    if not best:
        return False
    if int(best.get("fills") or 0) < 30 or float(best.get("net_pnl") or 0.0) <= 0:
        return False
    if "passive" in str(best.get("strategy")) and liquidity.get("passive_verdict") != "PAPER_READY_SPECIFIC_STRATEGY":
        return False
    return any(item.get("strategy") == best.get("strategy") and item.get("recommendation") == "interesting_signal" for item in validation)


def _tiny_live_ready(best: dict[str, Any] | None, validation: list[dict[str, Any]], liquidity: dict[str, Any]) -> bool:
    if not _paper_ready(best, validation, liquidity):
        return False
    if int(best.get("fills") or 0) < 100:
        return False
    # Live paper results are not yet integrated, so this must remain false.
    return False


def _message(status: str) -> str:
    return {
        "NOT_READY_DATA_INCOMPLETE": "Do not trade. Data collection or replay labels are incomplete.",
        "NOT_READY_NO_EDGE": "Do not trade. No clean strategy has survived replay after current parser/settlement versions.",
        "RESEARCH_READY_MORE_DATA_NEEDED": "Research can continue, but sample size/robustness is not enough for paper confidence.",
        "PAPER_READY_SPECIFIC_STRATEGY": "Paper testing may be justified for one specific strategy, after manual candidate review.",
        "TINY_LIVE_READY_SPECIFIC_STRATEGY": "Tiny live readiness gate appears satisfied, but live trading is still disabled and requires manual approval.",
    }.get(status, "Unknown readiness status.")
