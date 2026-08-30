from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.passive_fill_model import PassiveFillConfig, PassiveQuote, adverse_selection_cents, simulate_passive_fill
from config import PROJECT_ROOT, settings
from data.storage import Storage


@dataclass(frozen=True)
class LiquidityAnalysisResult:
    summary: dict[str, Any]
    markets: list[dict[str, Any]]
    adverse_selection_failures: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "markets": self.markets[:50], "adverse_selection_failures": self.adverse_selection_failures[:50]}

    def to_text(self) -> str:
        lines = [
            f"markets_analyzed={self.summary.get('markets_analyzed')}",
            f"snapshots={self.summary.get('snapshots')}",
            f"passive_verdict={self.summary.get('passive_verdict')}",
            f"message={self.summary.get('message')}",
        ]
        lines.append("Top persistent-spread markets:")
        for row in self.markets[:10]:
            lines.append(
                f"- {row['market_ticker']} avg_spread={row['average_spread']:.2f} med_spread={row['median_spread']:.2f} "
                f"opp={row['potential_passive_quote_opportunities']} fills={row['conservative_fills']} "
                f"adverse_score={row['adverse_selection_score']:.2f}"
            )
        return "\n".join(lines)


class LiquidityAnalyzer:
    def __init__(self, storage: Storage | None = None):
        self.storage = storage or Storage()

    def analyze(self, start: date | None = None, end: date | None = None, last_days: int | None = None, persist_exports: bool = True) -> LiquidityAnalysisResult:
        frame = self._load(start, end, last_days)
        if frame.empty:
            return LiquidityAnalysisResult({"markets_analyzed": 0, "snapshots": 0, "passive_verdict": "NOT_READY_DATA_INCOMPLETE", "message": "No recorded replay snapshots."}, [], [])
        rows: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        cfg = PassiveFillConfig(
            assume_touch_fill=settings.passive_assume_touch_fill,
            fill_haircut=settings.passive_default_fill_haircut,
            adverse_selection_penalty_cents=settings.passive_adverse_selection_penalty_cents,
            require_traded_through=settings.passive_require_traded_through,
            min_displayed_depth=settings.passive_min_displayed_depth,
        )
        for ticker, group in frame.groupby("market_ticker"):
            group = group.sort_values("ts").reset_index(drop=True)
            metrics, bad = _market_liquidity_metrics(ticker, group, cfg)
            rows.append(metrics)
            failures.extend(bad)
        ranked = sorted(rows, key=lambda item: (item["persistent_spread_score"], -item["adverse_selection_score"], item["conservative_fills"]), reverse=True)
        failures = sorted(failures, key=lambda item: item.get("adverse_selection_cents", 0))
        verdict = _passive_verdict(ranked)
        summary = {
            "markets_analyzed": len(ranked),
            "snapshots": int(len(frame)),
            "markets_with_persistent_spreads": sum(1 for item in ranked if item["persistent_spread_score"] > 0.25),
            "markets_with_conservative_fills": sum(1 for item in ranked if item["conservative_fills"] > 0),
            "average_adverse_selection_score": sum(item["adverse_selection_score"] for item in ranked) / len(ranked) if ranked else 0.0,
            "passive_verdict": verdict,
            "message": "Passive liquidity remains approximate; queue position is unknown and fills must beat future prices.",
        }
        if persist_exports:
            _export_liquidity(ranked, failures)
        return LiquidityAnalysisResult(summary, ranked, failures)

    def _load(self, start: date | None, end: date | None, last_days: int | None) -> pd.DataFrame:
        start, end = _date_window(start, end, last_days)
        clauses = ["1=1"]
        params: dict[str, Any] = {}
        if start:
            clauses.append("date(ts) >= :start")
            params["start"] = start.isoformat()
        if end:
            clauses.append("date(ts) <= :end")
            params["end"] = end.isoformat()
        return self.storage.fetch_sql(f"SELECT * FROM recorded_orderbook_replay_snapshots WHERE {' AND '.join(clauses)} ORDER BY market_ticker, ts", params)


def _market_liquidity_metrics(ticker: str, group: pd.DataFrame, cfg: PassiveFillConfig) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spread = pd.to_numeric(group.get("spread_cents", pd.Series(dtype=float)), errors="coerce")
    depth_bid = pd.to_numeric(group.get("depth_yes_bid_1", pd.Series(dtype=float)), errors="coerce").fillna(0)
    depth_ask = pd.to_numeric(group.get("depth_yes_ask_1", pd.Series(dtype=float)), errors="coerce").fillna(0)
    potential = group[spread >= settings.passive_min_spread_cents].copy()
    fills = 0
    touched = 0
    traded_through = 0
    adverse_values: list[float] = []
    failures: list[dict[str, Any]] = []
    for idx, row in potential.head(200).iterrows():
        mid = _num(row.get("yes_mid"))
        if mid is None:
            continue
        quote = PassiveQuote(ticker, "BUY_YES", max(1.0, mid - settings.passive_min_spread_cents / 2.0), 1.0, _parse_ts(row.get("ts")))
        future = group[group.index > idx].head(240)
        result = simulate_passive_fill(quote, future, cfg)
        if result.fill_type.value == "TOUCHED_ONLY_NO_FILL":
            touched += 1
        if result.filled:
            fills += 1
            if result.fill_type.value == "TRADED_THROUGH_FILL":
                traded_through += 1
            future_mid = _future_mid_after(future, result.fill_ts, 30)
            adverse = adverse_selection_cents("BUY_YES", float(result.fill_price or 0.0), future_mid)
            if adverse is not None:
                adverse_values.append(adverse)
                if adverse < 0:
                    failures.append({"market_ticker": ticker, "fill_ts": result.fill_ts, "fill_price": result.fill_price, "future_mid_30m": future_mid, "adverse_selection_cents": adverse, "fill_type": result.fill_type.value})
    persistence = float((spread >= settings.passive_min_spread_cents).mean()) if len(spread) else 0.0
    adverse_score = -sum(v for v in adverse_values if v < 0) / max(len(adverse_values), 1)
    return (
        {
            "market_ticker": ticker,
            "snapshots": int(len(group)),
            "average_spread": float(spread.mean()) if not spread.dropna().empty else 0.0,
            "median_spread": float(spread.median()) if not spread.dropna().empty else 0.0,
            "p90_spread": float(spread.quantile(0.9)) if not spread.dropna().empty else 0.0,
            "average_best_bid_depth": float(depth_bid.mean()),
            "average_best_ask_depth": float(depth_ask.mean()),
            "average_total_displayed_depth": float(pd.to_numeric(group.get("total_yes_bid_depth", pd.Series(dtype=float)), errors="coerce").fillna(0).add(pd.to_numeric(group.get("total_no_bid_depth", pd.Series(dtype=float)), errors="coerce").fillna(0)).mean()),
            "time_spread_ge_5c": float((spread >= 5).mean()) if len(spread) else 0.0,
            "time_spread_ge_8c": float((spread >= 8).mean()) if len(spread) else 0.0,
            "time_spread_ge_10c": float((spread >= 10).mean()) if len(spread) else 0.0,
            "time_spread_ge_15c": float((spread >= 15).mean()) if len(spread) else 0.0,
            "persistent_spread_score": persistence,
            "potential_passive_quote_opportunities": int(len(potential)),
            "touched_only_fills": int(touched),
            "conservative_fills": int(fills),
            "traded_through_fills": int(traded_through),
            "adverse_selection_score": float(adverse_score),
            "average_future_price_edge_cents": float(sum(adverse_values) / len(adverse_values)) if adverse_values else 0.0,
            "estimated_ev_after_adverse_penalty": float((sum(adverse_values) / len(adverse_values)) - settings.passive_adverse_selection_penalty_cents) if adverse_values else 0.0,
            "avoid_reason": "bad adverse selection" if adverse_score > 2 else "dead/low-fill market" if not fills else "",
        },
        failures,
    )


def _future_mid_after(future: pd.DataFrame, fill_ts: datetime | None, minutes: int) -> float | None:
    if fill_ts is None or future.empty:
        return None
    target = _utc_timestamp(fill_ts + timedelta(minutes=minutes))
    parsed_ts = pd.to_datetime(future["ts"], errors="coerce", utc=True)
    frame = future[parsed_ts >= target]
    if frame.empty:
        return None
    return _num(frame.iloc[0].get("yes_mid"))


def _passive_verdict(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "NOT_READY_DATA_INCOMPLETE"
    if not any(row["conservative_fills"] for row in rows):
        return "RESEARCH_READY_MORE_DATA_NEEDED"
    good = [row for row in rows if row["conservative_fills"] >= 5 and row["estimated_ev_after_adverse_penalty"] > 0]
    return "PAPER_READY_SPECIFIC_STRATEGY" if good else "NOT_READY_NO_EDGE"


def _export_liquidity(rows: list[dict[str, Any]], failures: list[dict[str, Any]]) -> None:
    reports = PROJECT_ROOT / "reports"
    reports.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(reports / "liquidity_candidates.csv", index=False)
    pd.DataFrame(failures).to_csv(reports / "adverse_selection_failures.csv", index=False)


def _date_window(start: date | None, end: date | None, last_days: int | None) -> tuple[date | None, date | None]:
    if last_days is None:
        return start, end
    end_date = end or date.today()
    return end_date - timedelta(days=max(last_days, 1)), end_date


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _utc_timestamp(value: datetime) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
