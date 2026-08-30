from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from data.storage import Storage


@dataclass(frozen=True)
class RecordedDataAudit:
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.payload

    def to_text(self) -> str:
        p = self.payload
        lines = [
            "Recorded data audit",
            f"timestamp: {p['ts']}",
            f"orderbook_snapshots_live count: {p['total_snapshots']}",
            f"unique markets recorded: {p['unique_markets']}",
            f"first snapshot timestamp: {p['first_snapshot_ts']}",
            f"last snapshot timestamp: {p['last_snapshot_ts']}",
            f"average snapshots per market: {p['average_snapshots_per_market']:.1f}",
            f"markets with <50 snapshots: {len(p['markets_fewer_than_50_snapshots'])}",
            f"markets with >100 snapshots: {p['markets_with_100_plus_snapshots']}",
            f"markets with >500 snapshots: {p['markets_with_500_plus_snapshots']}",
            f"missing/invalid orderbook rows: {p['missing_or_invalid_orderbook_rows']}",
            f"markets with parsed contracts: {p['markets_with_parsed_contracts']}",
            f"markets with settlement labels: {p['markets_with_settlements']}",
            f"markets missing settlement labels: {p['markets_without_settlements']}",
            f"markets missing parsed weather contract details: {p['markets_missing_parsed_contracts']}",
            f"weather observations date range in DB: {p['weather_observations_date_range']}",
            "",
            "Readiness:",
        ]
        for key, value in p["readiness"].items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", p["verdict"]])
        if p["markets_missing_settlement_labels"]:
            lines.append("")
            lines.append("Markets missing settlement labels:")
            lines.extend(f"- {ticker}" for ticker in p["markets_missing_settlement_labels"][:50])
        return "\n".join(lines)


class RecordedDataAuditor:
    def __init__(self, storage: Storage | None = None):
        self.storage = storage or Storage()

    def audit(self, persist: bool = True) -> RecordedDataAudit:
        self.storage.init_db()
        stats = self._market_stats()
        total_snapshots = int(stats["snapshots"].sum()) if not stats.empty else 0
        unique_markets = int(stats["market_ticker"].nunique()) if not stats.empty else 0
        first_ts = _iso_or_none(stats["first_ts"].min()) if not stats.empty else None
        last_ts = _iso_or_none(stats["last_ts"].max()) if not stats.empty else None
        average_snapshots = float(total_snapshots / unique_markets) if unique_markets else 0.0

        recorded = set(stats["market_ticker"].astype(str)) if not stats.empty else set()
        parsed = self._distinct("parsed_contracts", "market_ticker")
        labels = self._distinct("settlement_labels", "market_ticker")
        statuses = self._market_statuses(recorded)
        missing_settlements = sorted(recorded - labels)
        missing_contracts = sorted(recorded - parsed)
        invalid_rows = self._invalid_orderbook_rows()
        weather_range = self._weather_observation_range()

        stats_records = []
        if not stats.empty:
            for _, row in stats.sort_values(["snapshots", "market_ticker"], ascending=[False, True]).iterrows():
                ticker = str(row["market_ticker"])
                stats_records.append(
                    {
                        "market_ticker": ticker,
                        "snapshots": int(row["snapshots"]),
                        "first_ts": _iso_or_none(row["first_ts"]),
                        "last_ts": _iso_or_none(row["last_ts"]),
                        "status": statuses.get(ticker),
                        "average_spread": _float_or_none(row.get("avg_spread")),
                        "median_spread": _float_or_none(row.get("median_spread")),
                        "average_best_bid_depth": _float_or_none(row.get("avg_bid_depth")),
                        "average_best_ask_depth": _float_or_none(row.get("avg_ask_depth")),
                        "has_parsed_contract": ticker in parsed,
                        "has_settlement_label": ticker in labels,
                    }
                )

        markets_100 = int((stats["snapshots"] >= 100).sum()) if not stats.empty else 0
        markets_500 = int((stats["snapshots"] >= 500).sum()) if not stats.empty else 0
        readiness = {
            "signal_only_replay": _readiness(total_snapshots > 0 and bool(recorded & parsed)),
            "taker_replay": _readiness(total_snapshots > 0 and bool(recorded & parsed & labels)),
            "approximate_passive_replay": _readiness(markets_100 > 0 and bool(recorded & labels)),
            "recorded_full_orderbook_passive_replay": _readiness(markets_500 > 0 and bool(recorded & labels)),
        }
        verdict = _verdict(total_snapshots, unique_markets, len(recorded & parsed), len(recorded & labels), markets_100, markets_500)
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "total_snapshots": total_snapshots,
            "unique_markets": unique_markets,
            "first_snapshot_ts": first_ts,
            "last_snapshot_ts": last_ts,
            "snapshots_per_market": stats_records,
            "average_snapshots_per_market": average_snapshots,
            "markets_fewer_than_50_snapshots": [item["market_ticker"] for item in stats_records if item["snapshots"] < 50],
            "markets_with_100_plus_snapshots": markets_100,
            "markets_with_500_plus_snapshots": markets_500,
            "market_statuses": statuses,
            "average_spread_by_market": {item["market_ticker"]: item["average_spread"] for item in stats_records},
            "median_spread_by_market": {item["market_ticker"]: item["median_spread"] for item in stats_records},
            "average_best_bid_depth_by_market": {item["market_ticker"]: item["average_best_bid_depth"] for item in stats_records},
            "average_best_ask_depth_by_market": {item["market_ticker"]: item["average_best_ask_depth"] for item in stats_records},
            "missing_or_invalid_orderbook_rows": invalid_rows,
            "markets_with_parsed_contracts": len(recorded & parsed),
            "markets_with_settlements": len(recorded & labels),
            "markets_without_settlements": len(missing_settlements),
            "markets_missing_settlement_labels": missing_settlements,
            "markets_missing_parsed_contracts": len(missing_contracts),
            "markets_missing_parsed_contract_details": missing_contracts,
            "weather_observations_date_range": weather_range,
            "readiness": readiness,
            "verdict": verdict,
        }
        if persist:
            self.storage.insert_recorded_data_audit(
                {
                    "ts": _parse_ts(payload["ts"]),
                    "total_snapshots": total_snapshots,
                    "unique_markets": unique_markets,
                    "first_snapshot_ts": _parse_ts(first_ts),
                    "last_snapshot_ts": _parse_ts(last_ts),
                    "markets_with_settlements": len(recorded & labels),
                    "markets_without_settlements": len(missing_settlements),
                    "markets_with_100_plus_snapshots": markets_100,
                    "markets_with_500_plus_snapshots": markets_500,
                    "verdict": verdict,
                    "raw_json": json.dumps(payload, default=str),
                }
            )
        return RecordedDataAudit(payload)

    def _market_stats(self) -> pd.DataFrame:
        frame = self.storage.fetch_sql(
            """
            SELECT
                market_ticker,
                COUNT(*) AS snapshots,
                MIN(ts) AS first_ts,
                MAX(ts) AS last_ts,
                AVG(spread_cents) AS avg_spread,
                AVG(depth_yes_bid_1) AS avg_bid_depth,
                AVG(depth_yes_ask_1) AS avg_ask_depth
            FROM orderbook_snapshots_live
            GROUP BY market_ticker
            """
        )
        if frame.empty:
            return frame
        spreads = self.storage.fetch_sql("SELECT market_ticker, spread_cents FROM orderbook_snapshots_live WHERE spread_cents IS NOT NULL")
        if spreads.empty:
            frame["median_spread"] = None
            return frame
        medians = spreads.groupby("market_ticker")["spread_cents"].median().reset_index().rename(columns={"spread_cents": "median_spread"})
        return frame.merge(medians, on="market_ticker", how="left")

    def _distinct(self, table_name: str, column_name: str) -> set[str]:
        frame = self.storage.fetch_sql(f"SELECT DISTINCT {column_name} FROM {table_name} WHERE {column_name} IS NOT NULL")
        if frame.empty:
            return set()
        return set(frame[column_name].astype(str))

    def _market_statuses(self, tickers: set[str]) -> dict[str, str | None]:
        if not tickers:
            return {}
        markets = self.storage.fetch_table("markets", limit=200000)
        statuses: dict[str, str | None] = {}
        if markets.empty:
            return {ticker: None for ticker in tickers}
        for _, row in markets.iterrows():
            ticker = str(row.get("ticker") or "")
            if ticker not in tickers:
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            status = payload.get("status") or payload.get("market_status") or payload.get("result") or payload.get("settlement_status")
            statuses[ticker] = str(status) if status is not None else None
        for ticker in tickers:
            statuses.setdefault(ticker, None)
        return statuses

    def _invalid_orderbook_rows(self) -> int:
        frame = self.storage.fetch_sql(
            """
            SELECT COUNT(*) AS invalid_rows
            FROM orderbook_snapshots_live
            WHERE
                (yes_best_bid IS NULL AND yes_best_ask IS NULL AND no_best_bid IS NULL AND no_best_ask IS NULL)
                OR (spread_cents IS NOT NULL AND spread_cents < 0)
            """
        )
        return int(frame.iloc[0]["invalid_rows"]) if not frame.empty else 0

    def _weather_observation_range(self) -> str:
        frame = self.storage.fetch_sql("SELECT MIN(observed_at) AS first_observed_at, MAX(observed_at) AS last_observed_at, COUNT(*) AS rows FROM weather_observations")
        if frame.empty or int(frame.iloc[0]["rows"] or 0) == 0:
            return "none stored in weather_observations table"
        return f"{frame.iloc[0]['first_observed_at']} to {frame.iloc[0]['last_observed_at']}"


def _verdict(total_snapshots: int, unique_markets: int, parsed: int, settled: int, markets_100: int, markets_500: int) -> str:
    if total_snapshots == 0:
        return "Data readiness: NOT READY. No recorded live orderbook snapshots."
    if parsed == 0:
        return "Data readiness: NOT READY. Recorded markets are missing parsed weather contracts."
    if settled == 0:
        return f"Data readiness: NOT READY. Need settlement labels for recorded markets. Recorded {total_snapshots} snapshots across {unique_markets} markets."
    passive = "OK" if markets_500 > 0 else "partial" if markets_100 > 0 else "not ready"
    return f"Data readiness: OK for signal/taker replay, {passive} for passive replay, missing settlements for {unique_markets - settled} markets."


def _readiness(ready: bool) -> str:
    return "READY" if ready else "NOT_READY"


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
