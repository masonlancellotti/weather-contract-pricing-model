from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.recorded_audit import RecordedDataAuditor
from backtest.recorded_backtester import RecordedOrderbookBacktester, recommend_next_action
from config import PROJECT_ROOT
from data.storage import Storage


@dataclass(frozen=True)
class EdgeReportResult:
    path: Path
    recommendation: str
    top_candidates: int
    message: str

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "recommendation": self.recommendation,
            "top_candidates": self.top_candidates,
            "message": self.message,
        }


class EdgeReportGenerator:
    def __init__(self, storage: Storage | None = None):
        self.storage = storage or Storage()
        self.audit = RecordedDataAuditor(self.storage)
        self.backtester = RecordedOrderbookBacktester(self.storage)

    def generate(self, start: date | None = None, end: date | None = None, last_days: int | None = 3) -> EdgeReportResult:
        audit = self.audit.audit(persist=True).to_dict()
        sweep = self._latest_stored_sweep() or self.backtester.sweep(start=start, end=end, last_days=last_days, label_quality="primary")
        top = sweep.get("top_candidates", [])
        rejected = sweep.get("rejected_strategies", [])
        best = top[0] if top else None
        recommendation = recommend_next_action(audit, top, None, int(best.get("fills", 0)) if best else 0, best.get("robustness") if best else None)
        self._manual_review_exports()
        report_text = self._markdown(audit, sweep, recommendation, start, end, last_days)
        reports_dir = PROJECT_ROOT / "reports"
        reports_dir.mkdir(exist_ok=True)
        path = reports_dir / f"edge_report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
        path.write_text(report_text, encoding="utf-8")
        return EdgeReportResult(
            path=path,
            recommendation=recommendation,
            top_candidates=len(top),
            message="No reliable edge found yet." if not top else "Preliminary candidate only. Not enough sample size for real-money confidence.",
        )

    def _latest_stored_sweep(self) -> dict[str, Any] | None:
        rows = self.storage.fetch_sql(
            """
            SELECT *
            FROM recorded_strategy_sweeps
            WHERE COALESCE(is_stale, 0) = 0
            ORDER BY id DESC
            LIMIT 1000
            """
        )
        if rows.empty:
            return None
        summaries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _, row in rows.iterrows():
            key = str(row.get("parameter_hash") or f"{row.get('strategy')}-{row.get('mode')}-{row.get('params_json')}")
            if key in seen:
                continue
            seen.add(key)
            raw = row.get("raw_json")
            summary = None
            if raw:
                try:
                    summary = json.loads(raw).get("summary")
                except (TypeError, json.JSONDecodeError):
                    summary = None
            if not isinstance(summary, dict):
                summary = row.to_dict()
                if isinstance(summary.get("params_json"), str):
                    try:
                        summary["params"] = json.loads(summary["params_json"])
                    except json.JSONDecodeError:
                        summary["params"] = summary["params_json"]
            summaries.append(summary)
        top = [item for item in summaries if item.get("fills", 0) and item.get("net_pnl", 0) > 0]
        top.sort(key=lambda item: (item.get("net_pnl", 0), item.get("fills", 0)), reverse=True)
        rejected = [
            {
                "strategy": item.get("strategy"),
                "params": item.get("params"),
                "reason": item.get("robustness_verdict") or item.get("message") or "rejected",
                "summary": item,
            }
            for item in summaries
            if item not in top
        ]
        return {
            "strategy_variants_tested": len(summaries),
            "top_candidates": top[:10],
            "rejected_strategies": rejected[:50],
            "recommendation": "DO_NOT_TRADE_EDGE_NOT_FOUND" if not top else "PAPER_TEST_SPECIFIC_STRATEGY",
            "message": "No reliable edge found yet." if not top else "Preliminary candidates found. Validate sample size and paper results before real money.",
        }

    def _markdown(self, audit: dict[str, Any], sweep: dict[str, Any], recommendation: str, start: date | None, end: date | None, last_days: int | None) -> str:
        top = sweep.get("top_candidates", [])
        rejected = sweep.get("rejected_strategies", [])
        window = _window_label(start, end, last_days)
        lines = [
            f"# Kalshi Weather Edge Report",
            "",
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            f"Window: {window}",
            "",
            "## 1. Executive Summary",
            "",
        ]
        if top:
            lines.append("Preliminary candidate only. Not enough sample size for real-money confidence unless the robustness section says otherwise.")
        else:
            lines.append("No reliable edge found yet.")
        lines.extend(
            [
                f"Recommended next action: `{recommendation}`",
                "Real-money trading is not justified by this report unless the recommendation explicitly says `READY_FOR_TINY_LIVE_TEST`.",
                "",
                "## 2. Data Coverage",
                "",
                f"- Orderbook snapshots: {audit.get('total_snapshots', 0)}",
                f"- Markets recorded: {audit.get('unique_markets', 0)}",
                f"- First snapshot: {audit.get('first_snapshot_ts')}",
                f"- Last snapshot: {audit.get('last_snapshot_ts')}",
                f"- Markets with settlement labels: {audit.get('markets_with_settlements', 0)}",
                f"- Markets missing settlement labels: {audit.get('markets_without_settlements', 0)}",
                f"- Markets with 100+ snapshots: {audit.get('markets_with_100_plus_snapshots', 0)}",
                f"- Markets with 500+ snapshots: {audit.get('markets_with_500_plus_snapshots', 0)}",
                f"- Audit verdict: {audit.get('verdict')}",
                "",
                "## 3. Strategy Results",
                "",
            ]
        )
        if top:
            for item in top[:10]:
                robust = item.get("robustness", {})
                lines.extend(
                    [
                        f"### {item['strategy']} ({item['mode']})",
                        "",
                        f"- Params: `{item.get('params')}`",
                        f"- Signals: {item.get('signals', 0)}",
                        f"- Fills: {item.get('fills', 0)}",
                        f"- Net P&L: {item.get('net_pnl', 0):.2f} cents",
                        f"- Fees: {item.get('fees', 0):.2f} cents",
                        f"- Win rate: {item.get('win_rate', 0):.2%}",
                        f"- Robustness verdict: {robust.get('verdict', item.get('robustness_verdict'))}",
                        "",
                    ]
                )
        else:
            lines.append("No strategy produced a candidate that survived the basic filters.")
            lines.append("")
        lines.extend(["## 4. Robustness", ""])
        if top:
            best = top[0]
            robust = best.get("robustness", {})
            for key in [
                "two_x_fees_net_pnl",
                "worse_fill_1_net_pnl",
                "worse_fill_3_net_pnl",
                "exclude_best_trade_net_pnl",
                "exclude_top3_trades_net_pnl",
                "min_100_snapshots_net_pnl",
                "min_500_snapshots_net_pnl",
            ]:
                lines.append(f"- {key}: {robust.get(key)}")
        else:
            lines.append("No candidate reached robustness testing with meaningful fills.")
        lines.extend(["", "## Did the Edge Survive the Range/Bucket Semantics Fix?", ""])
        lines.extend(self._semantic_fix_section(top))
        lines.extend(["", "## 5. Best Opportunities Observed", ""])
        if top:
            lines.append("The command currently stores full trade rows in `backtest_trades`; inspect the latest recorded run for per-trade details.")
        else:
            lines.append("None.")
        lines.extend(["", "## 6. Worst Trades / False Positives", ""])
        if top:
            lines.append("Review negative `net_pnl` rows in `backtest_trades`; this report does not hide losing trades.")
        else:
            lines.append("No filled strategy candidate to analyze.")
        lines.extend(["", "## 7. What Would Have Made Money", ""])
        if top:
            lines.append("At least one variant had positive replay P&L. Treat this as preliminary until it survives sample size, worse-fill, and paper-trading checks.")
        else:
            lines.append("Nothing robust under the tested conservative assumptions.")
        lines.extend(["", "## 8. What To Do Next", ""])
        lines.extend(_next_steps(recommendation))
        lines.extend(["", "## Rejected Strategies", ""])
        for item in rejected[:20]:
            lines.append(f"- {item['strategy']} params `{item.get('params')}`: {item.get('reason')}")
        return "\n".join(lines) + "\n"

    def _semantic_fix_section(self, top: list[dict[str, Any]]) -> list[str]:
        labels = self.storage.fetch_table("settlement_labels", limit=500000)
        contracts = self.storage.fetch_table("parsed_contracts", limit=500000)
        runs = self.storage.fetch_table("backtest_runs", limit=500000)
        range_contracts = 0
        unknown_contracts = 0
        if not contracts.empty:
            latest: list[dict[str, Any]] = []
            seen: set[str] = set()
            for _, row in contracts.sort_values("id", ascending=False).iterrows():
                payload = row.get("payload")
                if not isinstance(payload, dict):
                    continue
                ticker = str(payload.get("market_ticker") or "")
                if ticker in seen:
                    continue
                seen.add(ticker)
                latest.append(payload)
            cframe = pd.DataFrame(latest)
            if not cframe.empty:
                range_contracts = int(cframe.get("contract_type", pd.Series(dtype=object)).eq("range_bucket").sum())
                unknown_contracts = int(cframe.get("contract_type", pd.Series(dtype=object)).eq("unknown").sum())
        stale_runs = int(runs.get("is_stale", pd.Series(dtype=float)).fillna(0).astype(int).sum()) if not runs.empty and "is_stale" in runs else 0
        range_labels = int(labels.get("contract_type", pd.Series(dtype=object)).eq("range_bucket").sum()) if not labels.empty else 0
        profit_by_contract_type = top[0].get("profit_by_contract_type", {}) if top else {}
        verdict = "Inconclusive; need more data."
        if not top:
            verdict = "No clean strategy candidate survived the current filters."
        elif profit_by_contract_type and sum(float(v or 0) for k, v in profit_by_contract_type.items() if k != "range_bucket") > 0:
            verdict = "Yes, edge survived on clean threshold contracts."
        elif profit_by_contract_type and float(profit_by_contract_type.get("range_bucket", 0) or 0) > 0:
            verdict = "Yes, but only on range buckets; requires manual verification."
        return [
            "- Parser/settlement version: `v2_range_bucket_semantics`",
            f"- Contracts currently parsed as range_bucket: {range_contracts}",
            f"- Settlement labels currently marked range_bucket: {range_labels}",
            f"- Unknown contracts excluded from primary interpretation: {unknown_contracts}",
            f"- Stale/invalid backtest runs marked: {stale_runs}",
            f"- Current best-candidate profit by contract_type: `{profit_by_contract_type}`",
            f"- Verdict: **{verdict}**",
            "- Old already_hit P&L is invalid unless regenerated with this parser and settlement version.",
        ]

    def _manual_review_exports(self) -> None:
        reports_dir = PROJECT_ROOT / "reports"
        reports_dir.mkdir(exist_ok=True)
        labels = self.storage.fetch_table("settlement_labels", limit=500000)
        trades = self.storage.fetch_table("backtest_trades", limit=500000)
        changed_path = reports_dir / "manual_review_changed_labels.csv"
        if not changed_path.exists() or changed_path.stat().st_size <= 2:
            pd.DataFrame().to_csv(changed_path, index=False)
        if trades.empty:
            pd.DataFrame().to_csv(reports_dir / "manual_review_top_trades.csv", index=False)
            pd.DataFrame().to_csv(reports_dir / "manual_review_suspicious_bucket_markets.csv", index=False)
        else:
            trades.sort_values("net_pnl", ascending=False).head(100).to_csv(reports_dir / "manual_review_top_trades.csv", index=False)
            trades[trades.get("reason", pd.Series(dtype=object)).fillna("").str.contains("range_bucket", case=False, na=False)].head(200).to_csv(
                reports_dir / "manual_review_suspicious_bucket_markets.csv",
                index=False,
            )
        if labels.empty:
            pd.DataFrame().to_csv(reports_dir / "manual_review_strategy_candidates.csv", index=False)
        else:
            labels[labels.get("contract_type", pd.Series(dtype=object)).eq("range_bucket")].head(500).to_csv(
                reports_dir / "manual_review_strategy_candidates.csv",
                index=False,
            )


def _window_label(start: date | None, end: date | None, last_days: int | None) -> str:
    if start or end:
        return f"{start or 'beginning'} to {end or 'end'}"
    if last_days:
        return f"last {last_days} days"
    return "all recorded data"


def _next_steps(recommendation: str) -> list[str]:
    if recommendation == "KEEP_COLLECTING_DATA":
        return [
            "- Keep the recorder running.",
            "- Build exact settlements after each market day resolves.",
            "- Re-run `python main.py build-recorded-replay --last-days 3` and `python main.py edge-report --last-days 3`.",
            "- Do not trade real money.",
        ]
    if recommendation == "FIX_DATA_PIPELINE":
        return [
            "- Build or repair settlement labels first.",
            "- Run `python main.py build-exact-settlements --start YYYY-MM-DD --end YYYY-MM-DD`.",
            "- Do not use P&L until labels exist.",
        ]
    if recommendation in {"PAPER_TEST_TINY", "PAPER_TEST_SPECIFIC_STRATEGY"}:
        return [
            "- Paper test only, with tiny assumed size.",
            "- Keep recording full orderbooks while paper testing.",
            "- Do not enable live trading.",
        ]
    if recommendation == "READY_FOR_TINY_LIVE_TEST":
        return [
            "- This is the only state where a tiny live test could be discussed.",
            "- Still require explicit implementation and confirmation before any live trading.",
        ]
    return ["- No reliable edge found yet.", "- Keep collecting or change strategy hypotheses.", "- Do not trade real money."]
