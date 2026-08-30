from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal

from backtest.fees import ConservativeFixedFeeModel, FeeModel
from data.storage import Storage
from parsing.weather_contract import WeatherContract

BacktestMode = Literal["taker", "signal", "passive"]
LabelQuality = Literal["primary", "exploratory", "all"]


@dataclass(frozen=True)
class BacktestSummary:
    run_id: int
    strategy: str
    mode: str
    markets: int
    replay_snapshots: int
    signals: int
    filled_trades: int
    gross_pnl: float
    fees: float
    net_pnl: float
    roi: float
    win_rate: float
    max_drawdown: float
    average_edge: float
    average_entry_price: float
    average_settlement_payout: float
    warning_count: int
    data_quality_score: float
    limitations: list[str]
    message: str
    markets_excluded_low_confidence: int
    label_source_breakdown: dict[str, int]
    replay_data_type: str
    execution_assumption: str
    settlement_label_quality: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class BacktestRunner:
    def __init__(self, storage: Storage | None = None, fee_model: FeeModel | None = None):
        self.storage = storage or Storage()
        self.fee_model = fee_model or ConservativeFixedFeeModel()

    def run(self, strategy: str, start: date | None = None, end: date | None = None, mode: BacktestMode = "taker", label_quality: LabelQuality = "primary") -> dict:
        strategy = _normalize_strategy(strategy)
        if strategy == "all":
            return {"runs": [self.run(item, start, end, mode, label_quality) for item in ["already_hit", "late_day_high_fade"]]}

        self.storage.init_db()
        snapshots = self._snapshots(start, end)
        if snapshots.empty:
            return self._record_no_data_run(strategy, start, end, mode, label_quality, "Cannot run real backtest: missing replay snapshots. Run build-replay after load-history/build-settlements.")
        if self.storage.fetch_table("historical_candlesticks", limit=1).empty:
            return self._record_no_data_run(strategy, start, end, mode, label_quality, "Cannot run real backtest: missing historical candlesticks.")

        contracts = self._contracts(start, end)
        all_labels = self._labels(start, end)
        labels, excluded_low_conf = _filter_labels_by_quality(all_labels, label_quality)
        source_breakdown = _label_source_breakdown(all_labels)
        signals: list[dict] = []
        trades: list[dict] = []
        warnings: list[str] = []
        for ticker, market_snaps in snapshots.groupby("market_ticker"):
            contract = contracts.get(ticker)
            label = labels.get(ticker)
            if contract is None or label is None:
                warnings.append(f"{ticker}: missing contract or settlement label")
                continue
            if label.get("yes_result") is None:
                warnings.append(f"{ticker}: settlement label has no yes_result")
                continue
            signal = self._first_signal(strategy, contract, label, market_snaps.sort_values("ts"))
            if not signal:
                continue
            signals.append(signal)
            if mode == "signal":
                trades.append(self._signal_record(signal, label))
                continue
            if mode == "passive":
                warnings.append("Approx passive mode is scaffolded only; no profitability claim made.")
                continue
            trade = self._execute_taker(signal, label)
            if trade:
                trades.append(trade)

        label_conf = [float(label.get("confidence") or 0.0) for label in labels.values()]
        avg_label_conf = sum(label_conf) / len(label_conf) if label_conf else 0.0
        data_quality = _run_data_quality(avg_label_conf, source_breakdown, snapshots)
        limitations = _limitations_for_run(source_breakdown, snapshots)
        run_payload = {
            "strategy": strategy,
            "mode": mode,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "limitations": limitations,
            "data_quality_score": data_quality,
            "warnings": warnings,
            "real_replay_data": True,
            "markets_excluded_low_confidence": excluded_low_conf,
            "label_source_breakdown": source_breakdown,
            "replay_data_type": _replay_data_type(snapshots),
            "execution_assumption": _execution_assumption(mode, snapshots),
            "settlement_label_quality": label_quality,
        }
        run_id = self.storage.insert_json(
            "backtest_runs",
            run_payload,
            run_name=f"{strategy}_{mode}_{start}_{end}_{datetime.now(timezone.utc).isoformat()}",
            strategy=strategy,
            start_date=run_payload["start"],
            end_date=run_payload["end"],
            mode=mode,
            data_quality_score=data_quality,
            limitations=json.dumps(limitations),
            replay_data_type=run_payload["replay_data_type"],
            execution_assumption=run_payload["execution_assumption"],
            settlement_label_quality=label_quality,
        )
        for trade in trades:
            trade["run_id"] = run_id
            self.storage.insert_backtest_trade(_trade_row(trade))

        summary = self._summarize(run_id, strategy, mode, len(contracts), len(snapshots), signals, trades, warnings, data_quality, limitations, excluded_low_conf, source_breakdown, run_payload["replay_data_type"], run_payload["execution_assumption"], label_quality)
        return summary.to_dict()

    def _record_no_data_run(self, strategy: str, start: date | None, end: date | None, mode: str, label_quality: str, message: str) -> dict:
        payload = {
            "strategy": strategy,
            "mode": mode,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "summary": message,
            "real_replay_data": False,
            "limitations": [message],
            "data_quality_score": 0.0,
            "replay_data_type": "NO_REPLAY_DATA",
            "execution_assumption": "NO_EXECUTION",
            "settlement_label_quality": label_quality,
        }
        run_id = self.storage.insert_json(
            "backtest_runs",
            payload,
            run_name=f"{strategy}_{mode}_{start}_{end}",
            strategy=strategy,
            start_date=payload["start"],
            end_date=payload["end"],
            mode=mode,
            data_quality_score=0.0,
            limitations=json.dumps(payload["limitations"]),
            replay_data_type=payload["replay_data_type"],
            execution_assumption=payload["execution_assumption"],
            settlement_label_quality=label_quality,
        )
        payload["run_id"] = run_id
        return payload

    def _first_signal(self, strategy: str, contract: WeatherContract, label: dict, snapshots) -> dict | None:
        for _, row in snapshots.iterrows():
            weather = _json(row.get("weather_features_json"))
            market = _json(row.get("market_features_json"))
            features = {**weather, **market}
            signal = None
            if strategy == "already_hit":
                signal = _already_hit_signal(contract, label, row, features)
            elif strategy == "late_day_high_fade":
                signal = _late_day_high_fade_signal(contract, label, row, features)
            if signal:
                return signal
        return None

    def _execute_taker(self, signal: dict, label: dict) -> dict | None:
        action = signal["action"]
        if action == "BUY_YES":
            entry = signal.get("yes_ask")
            payout = 100.0 if int(label["yes_result"]) == 1 else 0.0
        elif action == "BUY_NO":
            entry = signal.get("no_ask")
            payout = 100.0 if int(label["yes_result"]) == 0 else 0.0
        else:
            return None
        if entry is None:
            return None
        contracts = 1.0
        fees = self.fee_model.fee_cents(int(round(entry)), contracts)
        gross = (payout - entry) * contracts
        net = gross - fees
        return {
            **signal,
            "contracts": contracts,
            "entry_price": entry,
            "exit_price": payout,
            "settlement_value": label.get("settlement_value"),
            "yes_result": int(label["yes_result"]),
            "gross_pnl": gross,
            "fees": fees,
            "net_pnl": net,
            "settlement_payout": payout,
            "raw_json": json.dumps({"signal": signal, "label": _jsonable_label(label)}, default=str),
        }

    def _signal_record(self, signal: dict, label: dict) -> dict:
        return {
            **signal,
            "contracts": 0.0,
            "entry_price": signal.get("yes_ask") if signal["action"] == "BUY_YES" else signal.get("no_ask"),
            "exit_price": None,
            "settlement_value": label.get("settlement_value"),
            "yes_result": int(label["yes_result"]),
            "gross_pnl": 0.0,
            "fees": 0.0,
            "net_pnl": 0.0,
            "settlement_payout": None,
            "raw_json": json.dumps({"signal": signal, "label": _jsonable_label(label), "mode": "signal"}, default=str),
        }

    def _summarize(self, run_id: int, strategy: str, mode: str, markets: int, snapshot_count: int, signals: list[dict], trades: list[dict], warnings: list[str], data_quality: float, limitations: list[str], excluded_low_conf: int, source_breakdown: dict[str, int], replay_data_type: str, execution_assumption: str, label_quality: str) -> BacktestSummary:
        filled = [trade for trade in trades if trade.get("contracts", 0) > 0]
        gross = sum(float(t.get("gross_pnl") or 0.0) for t in filled)
        fees = sum(float(t.get("fees") or 0.0) for t in filled)
        net = sum(float(t.get("net_pnl") or 0.0) for t in filled)
        capital = sum(float(t.get("entry_price") or 0.0) * float(t.get("contracts") or 0.0) for t in filled)
        wins = sum(1 for t in filled if float(t.get("net_pnl") or 0.0) > 0)
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for trade in filled:
            equity += float(trade.get("net_pnl") or 0.0)
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
        message = "No trades under conservative execution assumptions."
        if filled and len(filled) < 30:
            message = "WARNING: sample size too small to infer robust edge."
        elif filled and net <= 0:
            message = "No robust edge found under conservative assumptions."
        elif filled:
            message = "Positive conservative taker result; candidate for tiny paper testing after robustness checks."
        if source_breakdown and source_breakdown.get("hourly_station_observations", 0) >= max(1, sum(source_breakdown.values()) // 2):
            warnings.append("Most settlement labels are fallback hourly observations, not exact NWS CLI reports.")
        if not source_breakdown.get("nws_daily_climate_report", 0):
            warnings.append("No exact NWS Daily Climate Report labels found in this run.")
        return BacktestSummary(
            run_id=run_id,
            strategy=strategy,
            mode=mode,
            markets=markets,
            replay_snapshots=snapshot_count,
            signals=len(signals),
            filled_trades=len(filled),
            gross_pnl=gross,
            fees=fees,
            net_pnl=net,
            roi=net / capital if capital else 0.0,
            win_rate=wins / len(filled) if filled else 0.0,
            max_drawdown=max_dd,
            average_edge=sum(float(t.get("edge_cents") or 0.0) for t in filled) / len(filled) if filled else 0.0,
            average_entry_price=sum(float(t.get("entry_price") or 0.0) for t in filled) / len(filled) if filled else 0.0,
            average_settlement_payout=sum(float(t.get("settlement_payout") or 0.0) for t in filled) / len(filled) if filled else 0.0,
            warning_count=len(warnings),
            data_quality_score=data_quality,
            limitations=limitations + warnings[:10],
            message=message,
            markets_excluded_low_confidence=excluded_low_conf,
            label_source_breakdown=source_breakdown,
            replay_data_type=replay_data_type,
            execution_assumption=execution_assumption,
            settlement_label_quality=label_quality,
        )

    def _snapshots(self, start: date | None, end: date | None):
        clauses = ["1=1"]
        params = {}
        if start:
            clauses.append("date(ts) >= :start")
            params["start"] = start.isoformat()
        if end:
            clauses.append("date(ts) <= :end")
            params["end"] = end.isoformat()
        return self.storage.fetch_sql(f"SELECT * FROM replay_snapshots WHERE {' AND '.join(clauses)} ORDER BY market_ticker, ts", params)

    def _contracts(self, start: date | None, end: date | None) -> dict[str, WeatherContract]:
        frame = self.storage.fetch_table("parsed_contracts", limit=100000)
        contracts: dict[str, WeatherContract] = {}
        if frame.empty:
            return contracts
        for _, row in frame.sort_values("id", ascending=False).iterrows():
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            contract = WeatherContract.model_validate(payload)
            if contract.market_ticker in contracts:
                continue
            if contract.local_date is None:
                continue
            if start and contract.local_date < start:
                continue
            if end and contract.local_date > end:
                continue
            contracts[contract.market_ticker] = contract
        return contracts

    def _labels(self, start: date | None, end: date | None) -> dict[str, dict]:
        clauses = ["1=1"]
        params = {}
        if start:
            clauses.append("local_date >= :start")
            params["start"] = start.isoformat()
        if end:
            clauses.append("local_date <= :end")
            params["end"] = end.isoformat()
        frame = self.storage.fetch_sql(f"SELECT * FROM settlement_labels WHERE {' AND '.join(clauses)}", params)
        if frame.empty:
            return {}
        return {str(row["market_ticker"]): row.to_dict() for _, row in frame.iterrows()}


def _already_hit_signal(contract: WeatherContract, label: dict, row, features: dict) -> dict | None:
    if float(label.get("confidence") or 0.0) < 0.75 or int(features.get("observations_count_so_far") or 0) < 3:
        return None
    if not features.get("is_threshold_already_hit_asof"):
        return None
    yes_ask = _num(row.get("yes_ask"))
    if yes_ask is None or yes_ask > 97:
        return None
    edge = 99.0 - yes_ask
    if edge < 2:
        return None
    return _base_signal(contract, row, "BUY_YES", 99.0, edge, "Already-hit threshold stale-price test.")


def _late_day_high_fade_signal(contract: WeatherContract, label: dict, row, features: dict) -> dict | None:
    if contract.variable_type != "high_temp" or float(label.get("confidence") or 0.0) < 0.6:
        return None
    if features.get("is_threshold_already_hit_asof"):
        return None
    local_hour = _num(features.get("local_hour"))
    gap = _num(features.get("threshold_gap_max_so_far"))
    trend = _num(features.get("temp_trend_1h")) or 0.0
    if local_hour is None or local_hour < 14 or gap is None or gap < 2 or trend > 0.5:
        return None
    fair_yes = _late_day_yes_fair(gap, local_hour, trend)
    no_fair = 100.0 - fair_yes
    no_ask = _num(row.get("no_ask"))
    if no_ask is None:
        yes_bid = _num(row.get("yes_bid"))
        no_ask = 100.0 - yes_bid if yes_bid is not None else None
    if no_ask is None:
        return None
    edge = no_fair - no_ask
    if edge < 7:
        return None
    return _base_signal(contract, row, "BUY_NO", fair_yes, edge, f"Late-day high fade without historical forecasts: gap {gap:.1f}F, local hour {local_hour:.1f}, trend {trend:.1f}F/hr.")


def _late_day_yes_fair(gap_f: float, local_hour: float, trend_1h: float) -> float:
    # Conservative observation-only heuristic. It avoids tiny fair values when
    # historical forecasts are unavailable.
    base = 45.0 - gap_f * 8.0 - max(local_hour - 14.0, 0.0) * 4.0 + max(trend_1h, 0.0) * 6.0
    return max(8.0, min(45.0, base))


def _base_signal(contract: WeatherContract, row, action: str, fair_yes: float, edge: float, reason: str) -> dict:
    return {
        "market_ticker": contract.market_ticker,
        "strategy": "already_hit" if action == "BUY_YES" and fair_yes >= 99 else "late_day_high_fade",
        "ts": _parse_dt(row.get("ts")),
        "action": action,
        "side": "yes" if action == "BUY_YES" else "no",
        "yes_bid": _num(row.get("yes_bid")),
        "yes_ask": _num(row.get("yes_ask")),
        "no_bid": _num(row.get("no_bid")),
        "no_ask": _num(row.get("no_ask")),
        "edge_cents": edge,
        "fair_yes_price": fair_yes,
        "reason": reason,
    }


def _trade_row(trade: dict) -> dict:
    return {
        "run_id": trade.get("run_id"),
        "market_ticker": trade.get("market_ticker"),
        "strategy": trade.get("strategy"),
        "pnl_cents": trade.get("net_pnl"),
        "payload": trade,
        "ts": trade.get("ts"),
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
        "reason": trade.get("reason"),
        "raw_json": trade.get("raw_json"),
    }


def _normalize_strategy(strategy: str) -> str:
    aliases = {"already_hit_threshold": "already_hit", "already_hit": "already_hit", "all": "all"}
    return aliases.get(strategy, strategy)


def _filter_labels_by_quality(labels: dict[str, dict], label_quality: str) -> tuple[dict[str, dict], int]:
    threshold = {"primary": 0.85, "exploratory": 0.65, "all": -1.0}.get(label_quality, 0.85)
    kept = {ticker: label for ticker, label in labels.items() if float(label.get("confidence") or 0.0) >= threshold}
    return kept, len(labels) - len(kept)


def _label_source_breakdown(labels: dict[str, dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in labels.values():
        source = str(label.get("exact_source_type") or label.get("source") or "unknown")
        counts[source] = counts.get(source, 0) + 1
    return counts


def _replay_data_type(snapshots) -> str:
    if "replay_data_type" in snapshots and snapshots["replay_data_type"].fillna("").eq("recorded_full_orderbook").any():
        return "RECORDED_FULL_ORDERBOOK_REPLAY"
    return "REAL_TAKER_REPLAY_CANDLESTICK"


def _execution_assumption(mode: str, snapshots) -> str:
    if mode == "signal":
        return "SIGNAL_ONLY_NO_EXECUTION"
    if mode == "passive":
        return "APPROX_PASSIVE_REPLAY_TRADES"
    if _replay_data_type(snapshots) == "RECORDED_FULL_ORDERBOOK_REPLAY":
        return "RECORDED_FULL_ORDERBOOK_REPLAY"
    return "REAL_TAKER_REPLAY_CANDLESTICK"


def _limitations_for_run(source_breakdown: dict[str, int], snapshots) -> list[str]:
    limitations: list[str] = []
    if _replay_data_type(snapshots) == "RECORDED_FULL_ORDERBOOK_REPLAY":
        limitations.append("Replay uses locally recorded full orderbook snapshots.")
    else:
        limitations.append("No full historical L2 orderbook. Taker fills use candlestick yes_ask/no_ask proxy.")
    if source_breakdown.get("nws_daily_climate_report"):
        limitations.append("Settlement labels use parsed NWS Daily Climate Report where available.")
    else:
        limitations.append("Settlement labels computed from hourly observations, not official NWS final climate report.")
    limitations.append("Historical forecasts unavailable; replay strategies use observations only.")
    return limitations


def _run_data_quality(avg_label_conf: float, source_breakdown: dict[str, int], snapshots) -> float:
    if not avg_label_conf:
        return 0.0
    if _replay_data_type(snapshots) == "RECORDED_FULL_ORDERBOOK_REPLAY" and avg_label_conf >= 0.95:
        return 1.0
    if source_breakdown.get("nws_daily_climate_report") and avg_label_conf >= 0.85:
        return 0.85
    if avg_label_conf >= 0.65:
        return 0.7
    return min(0.5, avg_label_conf)


def _json(value) -> dict:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _jsonable_label(label: dict) -> dict:
    return json.loads(json.dumps(label, default=str))
