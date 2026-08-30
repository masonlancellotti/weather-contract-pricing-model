from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from data.storage import Storage


@dataclass(frozen=True)
class SignalValidationResult:
    summary_by_strategy: list[dict[str, Any]]
    rows: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"summary_by_strategy": self.summary_by_strategy, "rows_preview": self.rows[:50]}

    def to_text(self) -> str:
        lines = ["Signal future-price validation:"]
        for row in self.summary_by_strategy:
            lines.append(
                f"- {row['strategy']} signals={row['signal_count']} beat_5m={row['beat_5m_pct']:.2%} "
                f"beat_30m={row['beat_30m_pct']:.2%} avg_future_edge={row['average_future_price_edge_cents']:.2f} recommendation={row['recommendation']}"
            )
        return "\n".join(lines)


class SignalValidator:
    def __init__(self, storage: Storage | None = None):
        self.storage = storage or Storage()

    def validate(self, start: date | None = None, end: date | None = None, last_days: int | None = None) -> SignalValidationResult:
        start, end = _date_window(start, end, last_days)
        trades = self._load_trades(start, end)
        replay = self._load_replay(start, end)
        rows: list[dict[str, Any]] = []
        if trades.empty or replay.empty:
            return SignalValidationResult([], [])
        replay_by_market = {ticker: group.sort_values("ts").reset_index(drop=True) for ticker, group in replay.groupby("market_ticker")}
        for _, trade in trades.iterrows():
            group = replay_by_market.get(str(trade.get("market_ticker")))
            if group is None:
                continue
            validation = future_price_validation_for_signal(trade.to_dict(), group)
            rows.append({**trade.to_dict(), **validation})
        summary = _summarize(rows)
        return SignalValidationResult(summary, rows)

    def _load_trades(self, start: date | None, end: date | None) -> pd.DataFrame:
        clauses = ["ts IS NOT NULL"]
        params: dict[str, Any] = {}
        if start:
            clauses.append("date(ts) >= :start")
            params["start"] = start.isoformat()
        if end:
            clauses.append("date(ts) <= :end")
            params["end"] = end.isoformat()
        return self.storage.fetch_sql(f"SELECT * FROM backtest_trades WHERE {' AND '.join(clauses)}", params)

    def _load_replay(self, start: date | None, end: date | None) -> pd.DataFrame:
        clauses = ["1=1"]
        params: dict[str, Any] = {}
        if start:
            clauses.append("date(ts) >= :start")
            params["start"] = start.isoformat()
        if end:
            clauses.append("date(ts) <= :end")
            params["end"] = end.isoformat()
        return self.storage.fetch_sql(f"SELECT market_ticker, ts, yes_mid FROM recorded_orderbook_replay_snapshots WHERE {' AND '.join(clauses)} ORDER BY market_ticker, ts", params)


def future_price_validation_for_signal(signal: dict[str, Any], market_replay: pd.DataFrame) -> dict[str, Any]:
    ts = _parse_ts(signal.get("ts"))
    if ts is None or market_replay.empty:
        return {}
    entry = _num(signal.get("entry_price") or signal.get("assumed_fill_price") or signal.get("intended_price"))
    action = str(signal.get("action") or "").upper()
    replay_ts = pd.to_datetime(market_replay["ts"], errors="coerce", utc=True)
    future = market_replay[replay_ts > _utc_timestamp(ts)].copy()
    result: dict[str, Any] = {}
    edges = []
    for minutes in [5, 15, 30, 60]:
        mid = _mid_after(future, ts + timedelta(minutes=minutes))
        result[f"future_mid_{minutes}m"] = mid
        beat = _beats_future(action, entry, mid)
        result[f"beat_{minutes}m"] = beat
        if entry is not None and mid is not None:
            edge = _directional_future_edge(action, entry, mid)
            result[f"future_price_edge_{minutes}m_cents"] = edge
            if edge is not None:
                edges.append(edge)
    final_mid = _num(future.iloc[-1].get("yes_mid")) if not future.empty else None
    result["final_mid_before_close"] = final_mid
    result["beat_close"] = _beats_future(action, entry, final_mid)
    result["future_price_edge_cents"] = sum(edges) / len(edges) if edges else None
    return result


def _summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    output: list[dict[str, Any]] = []
    for strategy, group in frame.groupby("strategy"):
        item = {"strategy": strategy, "signal_count": int(len(group))}
        for minutes in [5, 15, 30, 60]:
            values = group.get(f"beat_{minutes}m", pd.Series(dtype=object)).dropna()
            item[f"beat_{minutes}m_pct"] = float(values.mean()) if not values.empty else 0.0
        item["beat_close_pct"] = float(group.get("beat_close", pd.Series(dtype=object)).dropna().mean()) if "beat_close" in group else 0.0
        item["average_future_price_edge_cents"] = float(pd.to_numeric(group.get("future_price_edge_cents", pd.Series(dtype=float)), errors="coerce").dropna().mean() or 0.0)
        item["settlement_win_rate"] = float((pd.to_numeric(group.get("net_pnl", pd.Series(dtype=float)), errors="coerce") > 0).mean()) if "net_pnl" in group else 0.0
        item["net_pnl_if_taker"] = float(pd.to_numeric(group.get("net_pnl", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
        item["recommendation"] = "interesting_signal" if item["beat_30m_pct"] > 0.55 and item["average_future_price_edge_cents"] > 0 else "not_validated"
        output.append(item)
    return sorted(output, key=lambda item: item["average_future_price_edge_cents"], reverse=True)


def _mid_after(future: pd.DataFrame, target: datetime) -> float | None:
    future_ts = pd.to_datetime(future["ts"], errors="coerce", utc=True)
    later = future[future_ts >= _utc_timestamp(target)]
    if later.empty:
        return None
    return _num(later.iloc[0].get("yes_mid"))


def _beats_future(action: str, entry: float | None, future_yes_mid: float | None) -> bool | None:
    edge = _directional_future_edge(action, entry, future_yes_mid)
    return None if edge is None else edge > 0


def _directional_future_edge(action: str, entry: float | None, future_yes_mid: float | None) -> float | None:
    if entry is None or future_yes_mid is None:
        return None
    if "YES" in action:
        return future_yes_mid - entry
    if "NO" in action:
        return (100.0 - future_yes_mid) - entry
    return None


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
