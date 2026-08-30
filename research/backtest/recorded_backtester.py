from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from itertools import product
from typing import Any, Literal

import pandas as pd

from backtest.fees import ConservativeFixedFeeModel
from data.storage import Storage
from data.weather_settlement_loader import SETTLEMENT_VERSION
from parsing.weather_contract import PARSER_VERSION
from research.edge_types import confidence_level, edge_type_for_strategy

RecordedMode = Literal["default", "taker", "signal_only", "conservative_passive", "full_orderbook_passive_approx"]
LabelQuality = Literal["primary", "exploratory", "all"]
STRATEGY_VERSION = "v2_range_bucket_semantics"


@dataclass(frozen=True)
class RecordedBacktestResult:
    summary: dict[str, Any]
    trades: list[dict[str, Any]]
    signals: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {**self.summary, "trades": self.trades, "signals_preview": self.signals[:20]}


class RecordedOrderbookBacktester:
    def __init__(self, storage: Storage | None = None):
        self.storage = storage or Storage()

    def run(
        self,
        strategy: str,
        start: date | None = None,
        end: date | None = None,
        last_days: int | None = None,
        mode: RecordedMode = "default",
        label_quality: LabelQuality = "primary",
        params: dict[str, Any] | None = None,
        persist: bool = True,
        fee_cents: float = 1.0,
        worse_fill_cents: float = 0.0,
        min_snapshots_per_market: int = 0,
    ) -> dict[str, Any]:
        if strategy == "all":
            strategies = ["already_hit", "late_day_high_fade", "late_day_low_fade", "ladder_consistency", "wide_spread_passive"]
            return {
                "runs": [
                    self.run(item, start, end, last_days, mode, label_quality, persist=persist).get("summary", {})
                    for item in strategies
                ]
            }
        snapshots = self._load_snapshots(start, end, last_days, label_quality, min_snapshots_per_market)
        modes = ["taker", "signal_only"] if mode == "default" else [mode]
        if len(modes) > 1:
            return {
                "runs": [
                    self._evaluate(strategy, snapshots, run_mode, label_quality, params or {}, persist, fee_cents, worse_fill_cents).to_dict()
                    for run_mode in modes
                ]
            }
        return self._evaluate(strategy, snapshots, modes[0], label_quality, params or {}, persist, fee_cents, worse_fill_cents).to_dict()

    def sweep(self, start: date | None = None, end: date | None = None, last_days: int | None = None, label_quality: LabelQuality = "primary") -> dict[str, Any]:
        snapshots = self._load_snapshots(start, end, last_days, label_quality, 0)
        variants = _sweep_variants()
        results: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for variant in variants:
            result = self._evaluate(
                variant["strategy"],
                snapshots,
                variant["mode"],
                label_quality,
                variant["params"],
                persist=False,
                fee_cents=1.0,
                worse_fill_cents=0.0,
            )
            summary = result.summary
            if result.trades and summary["net_pnl"] > 0:
                robustness = self._robustness(variant, snapshots, label_quality, result.trades)
            else:
                robustness = {"verdict": _rejection_reason(summary), "two_x_fees_net_pnl": None, "worse_fill_1_net_pnl": None, "worse_fill_3_net_pnl": None}
            summary["robustness"] = robustness
            summary["robustness_verdict"] = robustness["verdict"]
            summary["recommendation"] = recommend_next_action(None, [summary], None, int(summary["fills"]), robustness)
            self.storage.insert_recorded_strategy_sweep(
                {
                    "ts": datetime.now(timezone.utc),
                    "strategy": summary["strategy"],
                    "mode": summary["mode"],
                    "params_json": json.dumps(summary["params"], default=str),
                    "start_date": start.isoformat() if start else None,
                    "end_date": end.isoformat() if end else None,
                    "label_quality": label_quality,
                    "markets": summary["markets"],
                    "snapshots": summary["snapshots"],
                    "signals": summary["signals"],
                    "fills": summary["fills"],
                    "gross_pnl": summary["gross_pnl"],
                    "fees": summary["fees"],
                    "net_pnl": summary["net_pnl"],
                    "roi": summary["roi"],
                    "win_rate": summary["win_rate"],
                    "max_drawdown": summary["max_drawdown"],
                    "robustness_verdict": summary["robustness_verdict"],
                    "recommendation": summary["recommendation"],
                    "parser_version": summary.get("parser_version"),
                    "settlement_version": summary.get("settlement_version"),
                    "strategy_version": summary.get("strategy_version"),
                    "parameter_hash": summary.get("parameter_hash"),
                    "is_stale": 0,
                    "raw_json": json.dumps({"summary": summary, "trades": result.trades[:100]}, default=str),
                }
            )
            if _is_candidate(summary):
                results.append(summary)
            else:
                rejected.append({"strategy": summary["strategy"], "params": summary["params"], "reason": _rejection_reason(summary), "summary": summary})
        ranked = sorted(results, key=_rank_key, reverse=True)
        rejected = sorted(rejected, key=lambda item: (item["summary"]["fills"], item["summary"]["net_pnl"]), reverse=True)
        top_candidates = ranked[:10]
        action = recommend_next_action(None, ranked, None, max((int(item["fills"]) for item in ranked), default=0), ranked[0].get("robustness") if ranked else None)
        return {
            "strategy_variants_tested": len(variants),
            "top_candidates": top_candidates,
            "rejected_strategies": rejected[:25],
            "recommendation": action,
            "message": "No reliable edge found yet." if not top_candidates else "Preliminary candidates found. Validate sample size and paper results before real money.",
        }

    def _evaluate(
        self,
        strategy: str,
        snapshots: pd.DataFrame,
        mode: str,
        label_quality: str,
        params: dict[str, Any],
        persist: bool,
        fee_cents: float,
        worse_fill_cents: float,
    ) -> RecordedBacktestResult:
        strategy = _normalize_strategy(strategy)
        if snapshots.empty:
            summary = _empty_summary(strategy, mode, params, label_quality, "Cannot run recorded backtest: missing recorded_orderbook_replay_snapshots. Run build-recorded-replay.")
            return RecordedBacktestResult(summary, [], [])
        signals: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        warnings: list[str] = []
        if strategy == "ladder_consistency":
            signals = _ladder_signals(snapshots, params)
            summary = _summarize(strategy, mode, params, snapshots, signals, trades, warnings + ["Ladder consistency is signal-only; no P&L claimed."], label_quality)
            return RecordedBacktestResult(summary, trades, signals)

        if strategy == "late_day_low_fade":
            warnings.append("LateDayLowFadeStrategy is scaffolded only for recorded replay; skipped to avoid unsafe low-temp time-profile assumptions.")
            summary = _summarize(strategy, mode, params, snapshots, signals, trades, warnings, label_quality)
            return RecordedBacktestResult(summary, trades, signals)

        for ticker, group in snapshots.groupby("market_ticker", sort=False):
            group = group.sort_values("ts").reset_index(drop=True)
            signal = _first_signal_for_market(strategy, group, params, mode)
            if signal is None:
                continue
            signal["execution_type"] = mode
            signals.append(signal)
            if mode == "signal_only":
                signal["future_validation"] = _future_validation(signal, group)
                continue
            if mode in {"conservative_passive", "full_orderbook_passive_approx"}:
                fill = _passive_fill(signal, group, mode, params)
                if fill is None:
                    continue
                signal = {**signal, **fill}
            signal["execution_type"] = mode
            trade = _execute(signal, fee_cents=fee_cents, worse_fill_cents=worse_fill_cents)
            if trade is not None:
                trades.append(trade)
        summary = _summarize(strategy, mode, params, snapshots, signals, trades, warnings, label_quality)
        if persist:
            self._persist(summary, trades)
        return RecordedBacktestResult(summary, trades, signals)

    def _load_snapshots(self, start: date | None, end: date | None, last_days: int | None, label_quality: str, min_snapshots_per_market: int) -> pd.DataFrame:
        self.storage.init_db()
        start, end = _date_window(start, end, last_days)
        clauses = ["1=1"]
        params: dict[str, Any] = {}
        if start:
            clauses.append("date(ts) >= :start")
            params["start"] = start.isoformat()
        if end:
            clauses.append("date(ts) <= :end")
            params["end"] = end.isoformat()
        threshold = {"primary": 0.85, "exploratory": 0.65, "all": -1.0}.get(label_quality, 0.85)
        if threshold >= 0:
            clauses.append("settlement_confidence >= :settlement_confidence")
            params["settlement_confidence"] = threshold
        frame = self.storage.fetch_sql(
            f"SELECT * FROM recorded_orderbook_replay_snapshots WHERE {' AND '.join(clauses)} ORDER BY market_ticker, ts",
            params,
        )
        if frame.empty or min_snapshots_per_market <= 0:
            return frame
        counts = frame.groupby("market_ticker")["id"].count()
        keep = set(counts[counts >= min_snapshots_per_market].index)
        return frame[frame["market_ticker"].isin(keep)].copy()

    def _robustness(self, variant: dict[str, Any], snapshots: pd.DataFrame, label_quality: str, base_trades: list[dict[str, Any]]) -> dict[str, Any]:
        if not base_trades:
            return {"verdict": "rejected: no fills", "two_x_fees_net_pnl": 0.0, "worse_fill_1_net_pnl": 0.0, "worse_fill_3_net_pnl": 0.0}
        net_values = sorted([float(t.get("net_pnl") or 0.0) for t in base_trades], reverse=True)
        net = sum(net_values)
        two_x_fees = sum(float(t.get("gross_pnl") or 0.0) - float(t.get("fees") or 0.0) * 2.0 for t in base_trades)
        worse1 = sum(float(t.get("net_pnl") or 0.0) - float(t.get("contracts") or 0.0) * 1.0 for t in base_trades)
        worse3 = sum(float(t.get("net_pnl") or 0.0) - float(t.get("contracts") or 0.0) * 3.0 for t in base_trades)
        exclude_best = net - (net_values[0] if net_values else 0.0)
        exclude_top3 = net - sum(net_values[:3])
        counts = snapshots.groupby("market_ticker")["id"].count() if not snapshots.empty else pd.Series(dtype=int)
        min100_markets = set(counts[counts >= 100].index)
        min500_markets = set(counts[counts >= 500].index)
        min100_net = sum(float(t.get("net_pnl") or 0.0) for t in base_trades if t.get("market_ticker") in min100_markets)
        min500_net = sum(float(t.get("net_pnl") or 0.0) for t in base_trades if t.get("market_ticker") in min500_markets)
        verdict = "passes basic robustness"
        if len(base_trades) < 30:
            verdict = "preliminary only: fewer than 30 fills"
        if net <= 0:
            verdict = "rejected: loses after fees"
        elif two_x_fees <= 0:
            verdict = "rejected: fails 2x fees"
        elif worse1 <= 0:
            verdict = "rejected: fails 1-cent worse fills"
        elif exclude_best <= 0:
            verdict = "rejected: dependent on best trade"
        return {
            "verdict": verdict,
            "two_x_fees_net_pnl": two_x_fees,
            "worse_fill_1_net_pnl": worse1,
            "worse_fill_3_net_pnl": worse3,
            "exclude_best_trade_net_pnl": exclude_best,
            "exclude_top3_trades_net_pnl": exclude_top3,
            "min_100_snapshots_net_pnl": min100_net,
            "min_500_snapshots_net_pnl": min500_net,
        }

    def _persist(self, summary: dict[str, Any], trades: list[dict[str, Any]]) -> None:
        run_id = self.storage.insert_json(
            "backtest_runs",
            summary,
            run_name=f"recorded_{summary['strategy']}_{summary['mode']}_{datetime.now(timezone.utc).isoformat()}",
            strategy=summary["strategy"],
            start_date=summary.get("start_date"),
            end_date=summary.get("end_date"),
            mode=summary["mode"],
            data_quality_score=summary["data_quality_score"],
            limitations=json.dumps(summary["limitations"], default=str),
            replay_data_type="RECORDED_FULL_ORDERBOOK_REPLAY",
            execution_assumption=summary["execution_assumption"],
            edge_type=summary.get("edge_type"),
            execution_type=summary.get("execution_type"),
            confidence_level=summary.get("confidence_level"),
            settlement_label_quality=summary["label_quality"],
            parser_version=summary.get("parser_version"),
            settlement_version=summary.get("settlement_version"),
            strategy_version=summary.get("strategy_version"),
            parameter_hash=summary.get("parameter_hash"),
            is_stale=0,
        )
        summary["run_id"] = run_id
        for trade in trades:
            trade["run_id"] = run_id
            self.storage.insert_backtest_trade(_trade_row(trade))


def _first_signal_for_market(strategy: str, group: pd.DataFrame, params: dict[str, Any], mode: str) -> dict[str, Any] | None:
    if strategy == "wide_spread_passive":
        return _wide_spread_signal(group, params)
    if strategy == "already_hit":
        return _already_hit_first(group, params)
    if strategy == "late_day_high_fade":
        return _late_day_high_fade_first(group, params)
    for _, row in group.iterrows():
        signal = None
        if signal is not None:
            return signal
    return None


def _already_hit_first(group: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any] | None:
    frame = group.copy()
    minutes = pd.to_numeric(frame["minutes_to_close"], errors="coerce")
    frame = frame[(minutes.isna() | (minutes > 0)) & frame["current_temp_asof"].notna()]
    frame = frame[pd.to_numeric(frame["settlement_confidence"], errors="coerce").fillna(0) >= float(params.get("required_settlement_confidence", 0.75))]
    for _, row in frame.iterrows():
        signal = _already_hit_signal(row, params)
        if signal is not None:
            return signal
    return None


def _late_day_high_fade_first(group: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any] | None:
    frame = group.copy()
    minutes = pd.to_numeric(frame["minutes_to_close"], errors="coerce")
    frame = frame[
        (minutes.isna() | (minutes > 0))
        & frame["variable_type"].eq("high_temp")
        & frame.get("contract_type", pd.Series("unknown", index=frame.index)).eq("threshold_above")
        & frame["comparator"].isin(["gt", "gte"])
        & pd.to_numeric(frame["local_hour"], errors="coerce").ge(float(params.get("min_local_hour", 14)))
        & pd.to_numeric(frame["threshold_gap_max_so_far"], errors="coerce").ge(float(params.get("min_gap", 2)))
        & pd.to_numeric(frame["temp_trend_1h"], errors="coerce").fillna(0).le(float(params.get("max_trend_1h", 0.5)))
        & frame["is_threshold_already_hit_asof"].fillna(0).astype(int).eq(0)
        & frame["no_best_ask"].notna()
    ]
    for _, row in frame.iterrows():
        signal = _late_day_high_fade_signal(row, params)
        if signal is not None:
            return signal
    return None


def _already_hit_signal(row, params: dict[str, Any]) -> dict[str, Any] | None:
    if not _is_before_close(row):
        return None
    max_ask = float(params.get("max_yes_ask_after_hit", 97))
    min_edge = float(params.get("min_edge_cents", 2))
    min_obs = int(params.get("min_observations_count", 3))
    if float(row.get("settlement_confidence") or 0.0) < float(params.get("required_settlement_confidence", 0.75)):
        return None
    resolved = _resolved_side_from_observed_threshold(row, float(params.get("strict_integer_buffer_f", 0.9)))
    if resolved is None:
        return None
    resolved_side, resolved_reason = resolved
    if _num(row.get("current_temp_asof")) is None:
        return None
    observations_count = _observations_from_raw(row)
    if observations_count < min_obs:
        return None
    if resolved_side == "yes":
        entry = _num(row.get("yes_best_ask"))
        action = "BUY_YES"
        fair_yes = 99.0
    else:
        entry = _num(row.get("no_best_ask"))
        action = "BUY_NO"
        fair_yes = 1.0
    if entry is None or entry > max_ask:
        return None
    edge = 99.0 - entry
    if edge < min_edge:
        return None
    return _signal(row, "already_hit", action, fair_yes, edge, f"{resolved_reason}; entry ask {entry:.1f} <= {max_ask:.1f}.")


def _late_day_high_fade_signal(row, params: dict[str, Any]) -> dict[str, Any] | None:
    if not _is_before_close(row):
        return None
    if row.get("variable_type") != "high_temp":
        return None
    if row.get("contract_type") != "threshold_above":
        return None
    if row.get("comparator") not in {"gt", "gte"}:
        return None
    local_hour = _num(row.get("local_hour"))
    gap = _num(row.get("threshold_gap_max_so_far"))
    trend = _num(row.get("temp_trend_1h")) or 0.0
    no_ask = _num(row.get("no_best_ask"))
    if local_hour is None or gap is None or no_ask is None:
        return None
    if local_hour < float(params.get("min_local_hour", 14)):
        return None
    if int(row.get("is_threshold_already_hit_asof") or 0) == 1:
        return None
    if gap < float(params.get("min_gap", 2)):
        return None
    if trend > float(params.get("max_trend_1h", 0.5)):
        return None
    fair_yes = _late_day_high_fair(gap, local_hour, trend)
    no_fair = 100.0 - fair_yes
    edge = no_fair - no_ask
    if fair_yes > float(params.get("max_yes_fair_for_no_trade", 35)):
        return None
    if edge < float(params.get("min_edge_cents", 7)):
        return None
    return _signal(row, "late_day_high_fade", "BUY_NO", fair_yes, edge, f"Late-day high fade: gap {gap:.1f}F, local hour {local_hour:.1f}, trend {trend:.1f}F/hr.")


def _resolved_side_from_observed_threshold(row, strict_integer_buffer_f: float = 0.9) -> tuple[str, str] | None:
    variable = row.get("variable_type")
    contract_type = row.get("contract_type") or "unknown"
    comparator = row.get("comparator")
    threshold = _num(row.get("threshold"))
    if contract_type == "range_bucket":
        range_low = _num(row.get("range_low"))
        range_high = _num(row.get("range_high"))
        if range_low is None or range_high is None:
            return None
        if variable == "high_temp":
            max_so_far = _num(row.get("max_temp_so_far_asof"))
            if max_so_far is None:
                return None
            if range_low <= max_so_far <= range_high:
                return None
            if max_so_far > range_high:
                return "no", "range_bucket_yes_impossible_high_exceeded"
        if variable == "low_temp":
            min_so_far = _num(row.get("min_temp_so_far_asof"))
            if min_so_far is None:
                return None
            if range_low <= min_so_far <= range_high:
                return None
            if min_so_far < range_low:
                return "no", "range_bucket_yes_impossible_low_breached"
        return None
    if contract_type == "unknown":
        return None
    if threshold is None:
        return None
    if variable == "high_temp":
        max_so_far = _num(row.get("max_temp_so_far_asof"))
        if max_so_far is None:
            return None
        high_strict_yes_threshold = threshold + strict_integer_buffer_f if _is_integer_threshold(threshold) else threshold
        if comparator == "gt" and max_so_far >= high_strict_yes_threshold:
            return "yes", "threshold_yes_already_guaranteed"
        if comparator == "gte" and max_so_far >= threshold:
            return "yes", "threshold_yes_already_guaranteed"
        if comparator == "lt" and max_so_far >= threshold:
            return "no", "threshold_yes_already_impossible_buy_no"
        if comparator == "lte" and max_so_far > threshold:
            return "no", "threshold_yes_already_impossible_buy_no"
    if variable == "low_temp":
        min_so_far = _num(row.get("min_temp_so_far_asof"))
        if min_so_far is None:
            return None
        low_strict_yes_threshold = threshold - strict_integer_buffer_f if _is_integer_threshold(threshold) else threshold
        if comparator == "lt" and min_so_far <= low_strict_yes_threshold:
            return "yes", "threshold_yes_already_guaranteed"
        if comparator == "lte" and min_so_far <= threshold:
            return "yes", "threshold_yes_already_guaranteed"
        if comparator == "gt" and min_so_far <= threshold:
            return "no", "threshold_yes_already_impossible_buy_no"
        if comparator == "gte" and min_so_far < threshold:
            return "no", "threshold_yes_already_impossible_buy_no"
    return None


def _is_integer_threshold(threshold: float) -> bool:
    return abs(threshold - round(threshold)) < 1e-9


def _wide_spread_signal(group: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any] | None:
    min_spread = float(params.get("min_spread", 8))
    quote_edge = float(params.get("quote_edge", 7))
    fee_buffer = float(params.get("fee_buffer", 2))
    penalty = float(params.get("adverse_selection_penalty", 2))
    for _, row in group.iterrows():
        if not _is_before_close(row):
            continue
        spread = _num(row.get("spread_cents"))
        if spread is None or spread < min_spread:
            continue
        fair_yes = _heuristic_fair_yes(row)
        yes_bid_quote = max(1.0, min(99.0, fair_yes - quote_edge - fee_buffer))
        no_bid_quote = max(1.0, min(99.0, (100.0 - fair_yes) - quote_edge - fee_buffer))
        yes_edge = fair_yes - yes_bid_quote - penalty
        no_edge = (100.0 - fair_yes) - no_bid_quote - penalty
        if yes_edge >= quote_edge and fair_yes > float(row.get("yes_mid") or 50):
            sig = _signal(row, "wide_spread_passive", "BUY_YES_LIMIT", fair_yes, yes_edge, f"Wide spread passive YES quote; spread {spread:.1f}.")
            sig["limit_price"] = yes_bid_quote
            sig["adverse_selection_penalty"] = penalty
            return sig
        if no_edge >= quote_edge and fair_yes < float(row.get("yes_mid") or 50):
            sig = _signal(row, "wide_spread_passive", "BUY_NO_LIMIT", fair_yes, no_edge, f"Wide spread passive NO quote; spread {spread:.1f}.")
            sig["limit_price"] = no_bid_quote
            sig["adverse_selection_penalty"] = penalty
            return sig
    return None


def _is_before_close(row) -> bool:
    minutes = _num(row.get("minutes_to_close"))
    return minutes is None or minutes > 0


def _passive_fill(signal: dict[str, Any], group: pd.DataFrame, mode: str, params: dict[str, Any]) -> dict[str, Any] | None:
    limit_price = float(signal["limit_price"])
    signal_ts = _parse_ts(signal["ts"])
    if signal_ts is None:
        return None
    traded_through = 1.0 if mode == "conservative_passive" else 2.0
    candidates = group[group["ts"].map(_parse_ts) > signal_ts].copy()
    for _, row in candidates.iterrows():
        if signal["action"] == "BUY_YES_LIMIT":
            ask = _num(row.get("yes_best_ask"))
            if ask is not None and ask <= limit_price - traded_through:
                return {"fill_ts": _parse_ts(row.get("ts")), "entry_price": min(99.0, limit_price + float(params.get("adverse_selection_penalty", 2)))}
        if signal["action"] == "BUY_NO_LIMIT":
            ask = _num(row.get("no_best_ask"))
            if ask is not None and ask <= limit_price - traded_through:
                return {"fill_ts": _parse_ts(row.get("ts")), "entry_price": min(99.0, limit_price + float(params.get("adverse_selection_penalty", 2)))}
    return None


def _execute(signal: dict[str, Any], fee_cents: float, worse_fill_cents: float) -> dict[str, Any] | None:
    action = signal["action"]
    if action in {"BUY_YES", "BUY_YES_LIMIT"}:
        entry = signal.get("entry_price", signal.get("yes_best_ask"))
        payout = 100.0 if int(signal["yes_result"]) == 1 else 0.0
        side = "yes"
    elif action in {"BUY_NO", "BUY_NO_LIMIT"}:
        entry = signal.get("entry_price", signal.get("no_best_ask"))
        payout = 100.0 if int(signal["yes_result"]) == 0 else 0.0
        side = "no"
    else:
        return None
    entry = _num(entry)
    if entry is None:
        return None
    entry = entry + worse_fill_cents
    if entry >= 100:
        return None
    contracts = 1.0
    fees = ConservativeFixedFeeModel(per_contract_cents=fee_cents).fee_cents(int(round(entry)), contracts)
    gross = payout - entry
    net = gross - fees
    return {
        **signal,
        "side": side,
        "contracts": contracts,
        "entry_price": entry,
        "exit_price": payout,
        "settlement_payout": payout,
        "gross_pnl": gross,
        "fees": fees,
        "net_pnl": net,
        "raw_json": json.dumps({"signal": signal, "fee_cents": fee_cents, "worse_fill_cents": worse_fill_cents}, default=str),
    }


def _summarize(strategy: str, mode: str, params: dict[str, Any], snapshots: pd.DataFrame, signals: list[dict[str, Any]], trades: list[dict[str, Any]], warnings: list[str], label_quality: str) -> dict[str, Any]:
    gross = sum(float(t.get("gross_pnl") or 0.0) for t in trades)
    fees = sum(float(t.get("fees") or 0.0) for t in trades)
    net = sum(float(t.get("net_pnl") or 0.0) for t in trades)
    capital = sum(float(t.get("entry_price") or 0.0) for t in trades)
    wins = sum(1 for t in trades if float(t.get("net_pnl") or 0.0) > 0)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in trades:
        equity += float(trade.get("net_pnl") or 0.0)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    source_breakdown = snapshots.get("settlement_source_type", pd.Series(dtype=object)).fillna("missing").value_counts().to_dict() if not snapshots.empty else {}
    limitations = ["Replay uses locally recorded full orderbook snapshots."]
    if mode in {"conservative_passive", "full_orderbook_passive_approx"}:
        limitations.append("Passive results are approximate: queue position and exact trade prints are unknown.")
    if not source_breakdown.get("nws_daily_climate_report", 0):
        limitations.append("No exact NWS Daily Climate Report labels in filtered replay rows.")
    if trades and len(trades) < 30:
        warnings.append("WARNING: sample size too small to infer robust edge.")
    message = "No reliable edge found yet."
    if trades and net > 0:
        message = "Preliminary candidate only. Not enough sample size for real-money confidence." if len(trades) < 30 else "Positive recorded replay result; requires robustness and paper validation."
    if signals and not trades and mode != "signal_only":
        message = "Signals found, but no fills under conservative execution assumptions."
    if mode == "signal_only":
        message = "Signal-only replay: no P&L claim."
    return {
        "strategy": strategy,
        "mode": mode,
        "params": params,
        "parser_version": PARSER_VERSION,
        "settlement_version": SETTLEMENT_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "parameter_hash": _parameter_hash(strategy, mode, label_quality, params),
        "is_stale": 0,
        "label_quality": label_quality,
        "markets": int(snapshots["market_ticker"].nunique()) if not snapshots.empty else 0,
        "snapshots": int(len(snapshots)),
        "signals": int(len(signals)),
        "fills": int(len(trades)),
        "gross_pnl": gross,
        "fees": fees,
        "net_pnl": net,
        "roi": net / capital if capital else 0.0,
        "win_rate": wins / len(trades) if trades else 0.0,
        "max_drawdown": max_dd,
        "average_entry_price": sum(float(t.get("entry_price") or 0.0) for t in trades) / len(trades) if trades else 0.0,
        "average_fair_value": sum(float(s.get("fair_yes_price") or 0.0) for s in signals) / len(signals) if signals else 0.0,
        "average_edge_cents": sum(float(s.get("edge_cents") or 0.0) for s in signals) / len(signals) if signals else 0.0,
        "average_spread": float(snapshots["spread_cents"].dropna().mean()) if not snapshots.empty and "spread_cents" in snapshots else 0.0,
        "average_depth": float(snapshots[["depth_yes_bid_1", "depth_yes_ask_1"]].fillna(0).mean().mean()) if not snapshots.empty else 0.0,
        "profit_by_market": _profit_by(trades, "market_ticker"),
        "profit_by_contract_type": _profit_by(trades, "contract_type"),
        "profit_by_hour": _profit_by_hour(trades),
        "profit_by_threshold_gap": _profit_by_gap(trades),
        "settlement_label_quality_breakdown": source_breakdown,
        "edge_type": edge_type_for_strategy(strategy),
        "execution_type": mode,
        "confidence_level": confidence_level(float(snapshots["data_quality_score"].dropna().mean()) if not snapshots.empty and "data_quality_score" in snapshots else 0.0),
        "data_quality_score": float(snapshots["data_quality_score"].dropna().mean()) if not snapshots.empty and "data_quality_score" in snapshots else 0.0,
        "warnings": warnings[:50],
        "limitations": limitations,
        "execution_assumption": _execution_assumption(mode),
        "message": message,
    }


def _signal(row, strategy: str, action: str, fair_yes: float, edge: float, reason: str) -> dict[str, Any]:
    return {
        "market_ticker": row.get("market_ticker"),
        "event_ticker": row.get("event_ticker"),
        "strategy": strategy,
        "edge_type": edge_type_for_strategy(strategy),
        "execution_type": None,
        "confidence_level": confidence_level(_num(row.get("data_quality_score"))),
        "data_quality_score": _num(row.get("data_quality_score")),
        "settlement_quality_score": _num(row.get("settlement_confidence")),
        "parser_version": row.get("parser_version"),
        "settlement_version": row.get("settlement_version"),
        "strategy_version": STRATEGY_VERSION,
        "ts": _parse_ts(row.get("ts")),
        "action": action,
        "contract_type": row.get("contract_type"),
        "yes_best_bid": _num(row.get("yes_best_bid")),
        "yes_best_ask": _num(row.get("yes_best_ask")),
        "no_best_bid": _num(row.get("no_best_bid")),
        "no_best_ask": _num(row.get("no_best_ask")),
        "spread_cents": _num(row.get("spread_cents")),
        "depth_yes_bid_1": _num(row.get("depth_yes_bid_1")),
        "depth_yes_ask_1": _num(row.get("depth_yes_ask_1")),
        "fair_yes_price": fair_yes,
        "edge_cents": edge,
        "settlement_value": _num(row.get("settlement_value")),
        "yes_result": int(row["yes_result"]) if row.get("yes_result") is not None else None,
        "settlement_confidence": _num(row.get("settlement_confidence")),
        "threshold_gap_max_so_far": _num(row.get("threshold_gap_max_so_far")),
        "local_hour": _num(row.get("local_hour")),
        "reason": reason,
    }


def _ladder_signals(snapshots: pd.DataFrame, params: dict[str, Any]) -> list[dict[str, Any]]:
    min_violation = float(params.get("min_violation_cents", 2))
    signals: list[dict[str, Any]] = []
    if snapshots.empty:
        return signals
    seen: set[tuple] = set()
    eligible = snapshots[snapshots["variable_type"].eq("high_temp")].copy()
    if "contract_type" in eligible.columns:
        eligible = eligible[eligible["contract_type"].eq("threshold_above")]
    for _, group in eligible.groupby(["event_ticker", "city", "local_date", "ts"], dropna=False):
        rows = group.dropna(subset=["threshold", "yes_best_bid", "yes_best_ask"]).sort_values("threshold")
        if len(rows) < 2:
            continue
        records = rows.to_dict("records")
        for lower, higher in zip(records, records[1:], strict=False):
            violation = float(higher["yes_best_bid"]) - float(lower["yes_best_ask"])
            key = (higher["market_ticker"], lower["market_ticker"])
            if violation >= min_violation and key not in seen:
                seen.add(key)
                signals.append(
                    {
                        "market_ticker": higher["market_ticker"],
                        "paired_market_ticker": lower["market_ticker"],
                        "strategy": "ladder_consistency",
                        "ts": _parse_ts(higher["ts"]),
                        "action": "RELATIVE_VALUE_FLAG",
                        "edge_cents": violation,
                        "fair_yes_price": None,
                        "reason": f"Executable monotonicity violation: higher threshold bid {higher['yes_best_bid']} > lower threshold ask {lower['yes_best_ask']} by {violation:.1f}c.",
                    }
                )
    return signals


def _future_validation(signal: dict[str, Any], group: pd.DataFrame) -> dict[str, Any]:
    ts = _parse_ts(signal.get("ts"))
    if ts is None:
        return {}
    future = group[group["ts"].map(_parse_ts) > ts].copy()
    validation: dict[str, Any] = {}
    for minutes in [30, 60]:
        target = ts + timedelta(minutes=minutes)
        later = future[future["ts"].map(_parse_ts) >= target]
        if not later.empty:
            validation[f"price_after_{minutes}m"] = _num(later.iloc[0].get("yes_mid"))
    if not future.empty:
        validation["final_available_yes_mid"] = _num(future.iloc[-1].get("yes_mid"))
    if signal["action"] in {"BUY_YES", "BUY_YES_LIMIT"}:
        validation["settlement_correct"] = signal.get("yes_result") == 1
    elif signal["action"] in {"BUY_NO", "BUY_NO_LIMIT"}:
        validation["settlement_correct"] = signal.get("yes_result") == 0
    return validation


def _late_day_high_fair(gap_f: float, local_hour: float, trend_1h: float) -> float:
    base = 45.0 - gap_f * 8.0 - max(local_hour - 14.0, 0.0) * 4.0 + max(trend_1h, 0.0) * 6.0
    return max(8.0, min(45.0, base))


def _heuristic_fair_yes(row) -> float:
    if row.get("contract_type") == "range_bucket":
        return 50.0
    if int(row.get("is_threshold_already_hit_asof") or 0) == 1:
        return 99.0
    if row.get("variable_type") == "high_temp":
        gap = _num(row.get("threshold_gap_max_so_far"))
        hour = _num(row.get("local_hour")) or 12.0
        trend = _num(row.get("temp_trend_1h")) or 0.0
        if gap is None:
            return 50.0
        return _late_day_high_fair(gap, hour, trend)
    if row.get("variable_type") == "low_temp":
        gap = _num(row.get("threshold_gap_min_so_far"))
        if gap is None:
            return 50.0
        return max(8.0, min(70.0, 50.0 - gap * 6.0))
    return 50.0


def _sweep_variants() -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    for max_ask in [90, 92, 95, 97, 98]:
        variants.append({"strategy": "already_hit", "mode": "taker", "params": {"max_yes_ask_after_hit": max_ask, "min_edge_cents": 2}})
    for local_hour, min_gap, edge in product([13, 14, 15, 16], [1, 2, 3, 4], [5, 7, 10, 12]):
        variants.append({"strategy": "late_day_high_fade", "mode": "taker", "params": {"min_local_hour": local_hour, "min_gap": min_gap, "min_edge_cents": edge, "max_trend_1h": 0.5}})
    for min_spread, quote_edge, penalty in product([6, 8, 10, 15], [5, 7, 10], [1, 2, 3, 5]):
        variants.append({"strategy": "wide_spread_passive", "mode": "conservative_passive", "params": {"min_spread": min_spread, "quote_edge": quote_edge, "fee_buffer": 2, "adverse_selection_penalty": penalty}})
    variants.append({"strategy": "ladder_consistency", "mode": "signal_only", "params": {"min_violation_cents": 2}})
    variants.append({"strategy": "late_day_low_fade", "mode": "taker", "params": {}})
    return variants


def recommend_next_action(audit_results: dict | None, sweep_results: list[dict] | None, settlement_quality: dict | None, sample_size: int, robustness: dict | None) -> str:
    if audit_results and audit_results.get("total_snapshots", 0) == 0:
        return "KEEP_COLLECTING_DATA"
    if audit_results and audit_results.get("markets_with_settlements", 0) == 0:
        return "FIX_DATA_PIPELINE"
    best = sweep_results[0] if sweep_results else None
    if not best:
        return "DO_NOT_TRADE_EDGE_NOT_FOUND"
    if sample_size < 30:
        return "PAPER_TEST_SPECIFIC_STRATEGY" if best.get("net_pnl", 0) > 0 and sample_size > 0 else "KEEP_COLLECTING_DATA"
    if best.get("net_pnl", 0) <= 0:
        return "DO_NOT_TRADE_EDGE_NOT_FOUND"
    if not robustness or robustness.get("two_x_fees_net_pnl", 0) <= 0 or robustness.get("worse_fill_1_net_pnl", 0) <= 0:
        return "PAPER_TEST_TINY"
    if robustness.get("exclude_best_trade_net_pnl", 0) <= 0:
        return "PAPER_TEST_TINY"
    return "PAPER_TEST_SPECIFIC_STRATEGY"


def _empty_summary(strategy: str, mode: str, params: dict, label_quality: str, message: str) -> dict[str, Any]:
    return {
        "strategy": strategy,
        "mode": mode,
        "params": params,
        "parser_version": PARSER_VERSION,
        "settlement_version": SETTLEMENT_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "parameter_hash": _parameter_hash(strategy, mode, label_quality, params),
        "is_stale": 0,
        "label_quality": label_quality,
        "markets": 0,
        "snapshots": 0,
        "signals": 0,
        "fills": 0,
        "gross_pnl": 0.0,
        "fees": 0.0,
        "net_pnl": 0.0,
        "roi": 0.0,
        "win_rate": 0.0,
        "max_drawdown": 0.0,
        "average_entry_price": 0.0,
        "average_fair_value": 0.0,
        "average_edge_cents": 0.0,
        "average_spread": 0.0,
        "average_depth": 0.0,
        "profit_by_market": {},
        "profit_by_hour": {},
        "profit_by_threshold_gap": {},
        "settlement_label_quality_breakdown": {},
        "data_quality_score": 0.0,
        "warnings": [message],
        "limitations": [message],
        "execution_assumption": _execution_assumption(mode),
        "message": message,
    }


def _trade_row(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": trade.get("run_id"),
        "market_ticker": trade.get("market_ticker"),
        "strategy": trade.get("strategy"),
        "pnl_cents": trade.get("net_pnl"),
        "payload": json.loads(json.dumps(trade, default=str)),
        "ts": trade.get("fill_ts") or trade.get("ts"),
        "action": trade.get("action"),
        "side": trade.get("side"),
        "contracts": trade.get("contracts"),
        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
        "settlement_value": trade.get("settlement_value"),
        "yes_result": trade.get("yes_result"),
        "gross_pnl": trade.get("gross_pnl"),
        "fees": trade.get("fees"),
        "net_pnl": trade.get("net_pnl"),
        "edge_cents": trade.get("edge_cents"),
        "fair_yes_price": trade.get("fair_yes_price"),
        "edge_type": trade.get("edge_type"),
        "execution_type": trade.get("execution_type"),
        "confidence_level": trade.get("confidence_level"),
        "data_quality_score": trade.get("data_quality_score"),
        "settlement_quality_score": trade.get("settlement_quality_score"),
        "parser_version": trade.get("parser_version"),
        "settlement_version": trade.get("settlement_version"),
        "strategy_version": trade.get("strategy_version"),
        "future_mid_5m": trade.get("future_mid_5m"),
        "future_mid_15m": trade.get("future_mid_15m"),
        "future_mid_30m": trade.get("future_mid_30m"),
        "future_mid_60m": trade.get("future_mid_60m"),
        "final_mid_before_close": trade.get("final_mid_before_close"),
        "beat_5m": trade.get("beat_5m"),
        "beat_15m": trade.get("beat_15m"),
        "beat_30m": trade.get("beat_30m"),
        "beat_60m": trade.get("beat_60m"),
        "beat_close": trade.get("beat_close"),
        "future_price_edge_cents": trade.get("future_price_edge_cents"),
        "reason": trade.get("reason"),
        "raw_json": trade.get("raw_json"),
    }


def _profit_by(trades: list[dict[str, Any]], key: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for trade in trades:
        item = str(trade.get(key) or "unknown")
        result[item] = result.get(item, 0.0) + float(trade.get("net_pnl") or 0.0)
    return result


def _profit_by_hour(trades: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for trade in trades:
        ts = _parse_ts(trade.get("ts"))
        hour = str(ts.hour) if ts else "unknown"
        result[hour] = result.get(hour, 0.0) + float(trade.get("net_pnl") or 0.0)
    return result


def _profit_by_gap(trades: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for trade in trades:
        gap = _num(trade.get("threshold_gap_max_so_far"))
        bucket = "unknown" if gap is None else f"{int(gap // 2 * 2)}-{int(gap // 2 * 2 + 2)}F"
        result[bucket] = result.get(bucket, 0.0) + float(trade.get("net_pnl") or 0.0)
    return result


def _filter_min_market_snapshots(snapshots: pd.DataFrame, minimum: int) -> pd.DataFrame:
    if snapshots.empty:
        return snapshots
    counts = snapshots.groupby("market_ticker")["id"].count()
    keep = set(counts[counts >= minimum].index)
    return snapshots[snapshots["market_ticker"].isin(keep)].copy()


def _is_candidate(summary: dict[str, Any]) -> bool:
    if summary["mode"] == "signal_only":
        return bool(summary["signals"])
    return summary["fills"] > 0 and summary["net_pnl"] > 0


def _rank_key(summary: dict[str, Any]) -> tuple:
    robust = summary.get("robustness", {})
    survives = int(_robust_value(robust, "two_x_fees_net_pnl") > 0) + int(_robust_value(robust, "worse_fill_1_net_pnl") > 0) + int(_robust_value(robust, "exclude_best_trade_net_pnl") > 0)
    return (survives, summary.get("net_pnl", 0), summary.get("fills", 0), summary.get("signals", 0))


def _robust_value(robust: dict[str, Any], key: str) -> float:
    value = robust.get(key)
    return 0.0 if value is None else float(value)


def _rejection_reason(summary: dict[str, Any]) -> str:
    if summary["signals"] == 0:
        return "no signals"
    if summary["fills"] == 0 and summary["mode"] != "signal_only":
        return "no fills"
    if summary["net_pnl"] <= 0 and summary["mode"] != "signal_only":
        return "loses after fees"
    if summary["fills"] < 30 and summary["mode"] != "signal_only":
        return "too few trades"
    robust = summary.get("robustness", {})
    if robust.get("exclude_best_trade_net_pnl", 1) <= 0:
        return "one outlier"
    return "data insufficient"


def _execution_assumption(mode: str) -> str:
    if mode == "signal_only":
        return "SIGNAL_ONLY_NO_EXECUTION"
    if mode == "taker":
        return "RECORDED_FULL_ORDERBOOK_TAKER"
    if mode == "conservative_passive":
        return "RECORDED_FULL_ORDERBOOK_PASSIVE_APPROX_CONSERVATIVE"
    if mode == "full_orderbook_passive_approx":
        return "RECORDED_FULL_ORDERBOOK_PASSIVE_APPROX_QUEUE_UNKNOWN"
    return "UNKNOWN"


def _parameter_hash(strategy: str, mode: str, label_quality: str, params: dict[str, Any]) -> str:
    payload = {
        "strategy": strategy,
        "mode": mode,
        "label_quality": label_quality,
        "params": params,
        "parser_version": PARSER_VERSION,
        "settlement_version": SETTLEMENT_VERSION,
        "strategy_version": STRATEGY_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _observations_from_raw(row) -> int:
    raw = row.get("raw_json")
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return 0
    if "weather_observations_count" in payload:
        return int(payload.get("weather_observations_count") or 0)
    weather = payload.get("weather", {})
    return int(weather.get("observations_count_so_far") or 0)


def _normalize_strategy(strategy: str) -> str:
    aliases = {"already_hit_threshold": "already_hit", "passive": "wide_spread_passive"}
    return aliases.get(strategy, strategy)


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


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
