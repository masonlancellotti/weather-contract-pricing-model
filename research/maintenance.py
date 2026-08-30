from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.recorded_backtester import RecordedOrderbookBacktester
from backtest.recorded_replay import RecordedOrderbookReplayBuilder
from backtest.edge_report import EdgeReportGenerator
from config import PROJECT_ROOT, settings
from data.kalshi_market_loader import is_likely_weather_market
from data.storage import Storage
from data.weather_settlement_loader import SETTLEMENT_VERSION, WeatherSettlementLoader, evaluate_contract_result
from parsing.market_parser import WeatherMarketParser
from parsing.weather_contract import PARSER_VERSION, WeatherContract
from research.liquidity_analysis import LiquidityAnalyzer
from research.signal_validation import SignalValidator
from research.trading_readiness import TradingReadiness


@dataclass(frozen=True)
class CommandResult:
    payload: dict[str, Any]

    def to_text(self) -> str:
        return json.dumps(self.payload, indent=2, default=str)


class ProjectMaintenance:
    def __init__(self, storage: Storage | None = None):
        self.storage = storage or Storage()

    def project_status(self) -> CommandResult:
        self.storage.init_db()
        counts = {table: self._count(table) for table in _key_tables()}
        latest_audit = self._latest_value("recorded_data_audits", "verdict")
        reports = sorted((PROJECT_ROOT / "reports").glob("edge_report_*.md"), reverse=True)
        latest_snapshot = self._latest_value("orderbook_snapshots_live", "ts")
        stale_runs = self._count_where("backtest_runs", "COALESCE(is_stale, 0) = 1")
        recent_warning = None
        if latest_snapshot:
            parsed = _parse_ts(latest_snapshot)
            if parsed and datetime.now(timezone.utc) - parsed > timedelta(minutes=30):
                recent_warning = "No orderbook snapshot in 30+ minutes; recorder may be stopped."
        payload = {
            "docs": str(PROJECT_ROOT / "docs"),
            "database": settings.sqlite_path,
            "counts": counts,
            "latest_audit_verdict": latest_audit,
            "latest_edge_report": str(reports[0]) if reports else None,
            "latest_recommendation": self._latest_sweep_recommendation(),
            "latest_orderbook_snapshot": latest_snapshot,
            "recorder_warning": recent_warning,
            "stale_backtest_runs": stale_runs,
            "parser_version": PARSER_VERSION,
            "settlement_version": SETTLEMENT_VERSION,
            "warning": "Read docs/CODEX_HANDOFF.md before trusting or changing strategy output.",
        }
        return CommandResult(payload)

    def reparse_contracts(self, weather_only: bool = True) -> CommandResult:
        self.storage.init_db()
        frame = self.storage.fetch_table("markets", limit=500000)
        parser = WeatherMarketParser()
        total = 0
        parsed = 0
        range_buckets = 0
        unknown = 0
        for _, row in frame.iterrows():
            market = row.get("payload")
            if not isinstance(market, dict):
                continue
            if weather_only and not is_likely_weather_market(market):
                continue
            total += 1
            contract = parser.parse(market)
            self.storage.save_parsed_contract(contract)
            parsed += 1
            if contract.contract_type == "range_bucket":
                range_buckets += 1
            if contract.contract_type == "unknown":
                unknown += 1
        return CommandResult(
            {
                "markets_considered": total,
                "contracts_parsed": parsed,
                "range_bucket_contracts": range_buckets,
                "unknown_contracts": unknown,
                "parser_version": PARSER_VERSION,
            }
        )

    def rebuild_settlement_labels(self, start: date | None = None, end: date | None = None, market_ticker: str | None = None) -> CommandResult:
        before = self._settlement_yes_results()
        result = WeatherSettlementLoader(storage=self.storage).build_settlements(start=start, end=end, market_ticker=market_ticker)
        after = self._settlement_yes_results()
        changed = []
        for ticker, old in before.items():
            if ticker in after and after[ticker] != old:
                changed.append({"market_ticker": ticker, "old_yes_result": old, "new_yes_result": after[ticker]})
        self._export_changed_labels(changed)
        return CommandResult({**result.to_dict(), "yes_results_changed": len(changed), "settlement_version": SETTLEMENT_VERSION})

    def validate_settlement_labels(self, weather_only: bool = True) -> CommandResult:
        labels = self.storage.fetch_table("settlement_labels", limit=500000)
        if labels.empty:
            return CommandResult({"total_labels": 0, "errors": ["no settlement labels"]})
        errors: list[dict[str, Any]] = []
        for _, row in labels.iterrows():
            if row.get("yes_result") is None:
                errors.append({"market_ticker": row.get("market_ticker"), "reason": "missing yes_result"})
            if str(row.get("contract_type") or "") == "range_bucket" and (pd.isna(row.get("range_low")) or pd.isna(row.get("range_high"))):
                errors.append({"market_ticker": row.get("market_ticker"), "reason": "range bucket missing bounds"})
        return CommandResult(
            {
                "total_labels": int(len(labels)),
                "primary_confidence_labels": int((pd.to_numeric(labels["confidence"], errors="coerce").fillna(0) >= 0.85).sum()),
                "range_bucket_labels": int(labels.get("contract_type", pd.Series(dtype=object)).eq("range_bucket").sum()),
                "errors": errors[:100],
                "excluded_from_primary": int((pd.to_numeric(labels["confidence"], errors="coerce").fillna(0) < 0.85).sum()),
            }
        )

    def validate_settlement_sources(self, fix: bool = True) -> CommandResult:
        self.storage.init_db()
        labels = self.storage.fetch_table("settlement_labels", limit=500000)
        if labels.empty:
            return CommandResult({"total_labels": 0})
        exact_mask = labels.get("exact_source_available", pd.Series(dtype=int)).fillna(0).astype(int).eq(1)
        source = labels.get("source", pd.Series(dtype=object)).fillna("")
        exact_type = labels.get("exact_source_type", pd.Series(dtype=object)).fillna("")
        inconsistent = labels[exact_mask & (source.ne(exact_type))]
        if fix and not inconsistent.empty:
            self.storage.execute(
                """
                UPDATE settlement_labels
                SET source = COALESCE(NULLIF(exact_source_type, ''), 'nws_daily_climate_report'),
                    primary_source_type = COALESCE(NULLIF(exact_source_type, ''), 'nws_daily_climate_report')
                WHERE COALESCE(exact_source_available, 0) = 1
                """
            )
        diffs = labels[pd.to_numeric(labels.get("exact_vs_fallback_diff", pd.Series(dtype=float)), errors="coerce").fillna(0).abs() > 0.01]
        return CommandResult(
            {
                "total_labels": int(len(labels)),
                "exact_nws_labels": int(exact_mask.sum()),
                "fallback_labels": int((~exact_mask).sum()),
                "inconsistent_source_metadata": int(len(inconsistent)),
                "fixed_inconsistent_source_metadata": int(len(inconsistent)) if fix else 0,
                "exact_and_fallback_differ": int(len(diffs)),
                "excluded_from_primary": int((pd.to_numeric(labels["confidence"], errors="coerce").fillna(0) < 0.85).sum()),
            }
        )

    def mark_stale_runs(self, reason: str = "Invalidated by v2 range/bucket contract semantics fix.") -> CommandResult:
        self.storage.init_db()
        self.storage.execute(
            """
            UPDATE backtest_runs
            SET is_stale = 1, stale_reason = :reason
            WHERE COALESCE(parser_version, '') != :parser_version
               OR COALESCE(settlement_version, '') != :settlement_version
            """,
            {"reason": reason, "parser_version": PARSER_VERSION, "settlement_version": SETTLEMENT_VERSION},
        )
        self.storage.execute(
            """
            UPDATE recorded_strategy_sweeps
            SET is_stale = 1, stale_reason = :reason
            WHERE COALESCE(parser_version, '') != :parser_version
               OR COALESCE(settlement_version, '') != :settlement_version
            """,
            {"reason": reason, "parser_version": PARSER_VERSION, "settlement_version": SETTLEMENT_VERSION},
        )
        return CommandResult(
            {
                "stale_backtest_runs": self._count_where("backtest_runs", "COALESCE(is_stale, 0) = 1"),
                "stale_recorded_sweeps": self._count_where("recorded_strategy_sweeps", "COALESCE(is_stale, 0) = 1"),
                "reason": reason,
            }
        )

    def diagnose_settlement_skips(self, last_days: int = 7) -> CommandResult:
        contracts = self._latest_contracts(last_days=last_days)
        labels = set(self.storage.fetch_sql("SELECT market_ticker FROM settlement_labels")["market_ticker"].astype(str)) if self._count("settlement_labels") else set()
        skipped = contracts[~contracts["market_ticker"].isin(labels)].copy() if not contracts.empty else contracts
        if skipped.empty:
            return CommandResult({"total_skipped": 0, "verdict": "No skipped settlement labels in selected window."})
        for column in ["city", "station_code", "variable_type", "contract_type", "local_date", "parse_confidence"]:
            if column not in skipped.columns:
                skipped[column] = None
        skipped["reason"] = skipped.apply(_skip_reason, axis=1)
        payload = {
            "total_skipped": int(len(skipped)),
            "skipped_by_city": skipped.get("city", pd.Series(dtype=object)).fillna("unknown").value_counts().to_dict(),
            "skipped_by_station": skipped.get("station_code", pd.Series(dtype=object)).fillna("missing").value_counts().to_dict(),
            "skipped_by_reason": skipped["reason"].value_counts().to_dict(),
            "top_skipped": skipped[["market_ticker", "city", "station_code", "variable_type", "contract_type", "local_date", "reason", "parse_confidence"]].head(20).to_dict("records"),
            "suggested_fixes": _suggested_skip_fixes(skipped),
        }
        return CommandResult(payload)

    def debug_city_settlements(self, city: str, last_days: int = 10) -> CommandResult:
        contracts = self._latest_contracts(last_days=last_days)
        if contracts.empty:
            return CommandResult({"city": city, "markets": []})
        for column in ["market_ticker", "title", "city", "station_code", "local_date", "variable_type", "contract_type"]:
            if column not in contracts.columns:
                contracts[column] = None
        mask = contracts.get("city", pd.Series(dtype=object)).fillna("").str.lower().str.contains(city.lower()) | contracts["market_ticker"].str.lower().str.contains(city.lower())
        rows = contracts[mask].copy()
        labels = self.storage.fetch_table("settlement_labels", limit=500000)
        if not labels.empty:
            rows = rows.merge(labels[["market_ticker", "source", "primary_source_type", "confidence", "warnings"]], on="market_ticker", how="left")
        for column in ["source", "primary_source_type", "confidence", "warnings"]:
            if column not in rows.columns:
                rows[column] = None
        return CommandResult(
            {
                "city_query": city,
                "markets_found": int(len(rows)),
                "markets": rows[["market_ticker", "title", "city", "station_code", "local_date", "variable_type", "contract_type", "source", "primary_source_type", "confidence", "warnings"]].head(100).to_dict("records") if not rows.empty else [],
                "note": "For Austin/AUS, expected default station is KAUS unless Kalshi rules explicitly name another station.",
            }
        )

    def validate_orderbook_depths(self, last_days: int = 3) -> CommandResult:
        start = (date.today() - timedelta(days=max(last_days, 1))).isoformat()
        frame = self.storage.fetch_sql(
            """
            SELECT market_ticker, ts, yes_best_bid, yes_best_ask, depth_yes_bid_1, depth_yes_ask_1,
                   total_yes_bid_depth, total_no_bid_depth, yes_bids_json, no_bids_json, raw_json
            FROM orderbook_snapshots_live
            WHERE date(ts) >= :start
            ORDER BY MAX(COALESCE(depth_yes_bid_1,0), COALESCE(depth_yes_ask_1,0), COALESCE(total_yes_bid_depth,0), COALESCE(total_no_bid_depth,0)) DESC
            LIMIT 50
            """,
            {"start": start},
        )
        if frame.empty:
            return CommandResult({"rows_checked": 0, "verdict": "No orderbook rows in selected window."})
        max_depth = float(frame[["depth_yes_bid_1", "depth_yes_ask_1", "total_yes_bid_depth", "total_no_bid_depth"]].fillna(0).max().max())
        extreme_10k = int(((frame[["depth_yes_bid_1", "depth_yes_ask_1", "total_yes_bid_depth", "total_no_bid_depth"]].fillna(0)) > 10000).any(axis=1).sum())
        rows = []
        for _, row in frame.iterrows():
            rows.append(
                {
                    "market_ticker": row["market_ticker"],
                    "ts": row["ts"],
                    "yes_best_bid": row["yes_best_bid"],
                    "yes_best_ask": row["yes_best_ask"],
                    "depth_yes_bid_1": row["depth_yes_bid_1"],
                    "depth_yes_ask_1": row["depth_yes_ask_1"],
                    "total_yes_bid_depth": row["total_yes_bid_depth"],
                    "total_no_bid_depth": row["total_no_bid_depth"],
                    "raw_yes_bids_snippet": _json_snippet(row.get("yes_bids_json")),
                    "raw_no_bids_snippet": _json_snippet(row.get("no_bids_json")),
                }
            )
        verdict = "Depth fields appear to be Kalshi contract quantities; queue position remains unknown."
        if extreme_10k:
            verdict = "Extreme depth values detected. Treat passive fill estimates cautiously until raw rows are manually reviewed."
        return CommandResult({"rows_checked": int(len(frame)), "max_depth": max_depth, "rows_over_10000": extreme_10k, "top_depth_rows": rows, "verdict": verdict})

    def collector_health(self, last_hours: int = 24) -> CommandResult:
        self.storage.init_db()
        since = (datetime.now(timezone.utc) - timedelta(hours=last_hours)).strftime("%Y-%m-%d %H:%M:%S")
        frame = self.storage.fetch_sql("SELECT * FROM orderbook_snapshots_live WHERE ts >= :since", {"since": since})
        last_ts = self._latest_value("orderbook_snapshots_live", "ts")
        state_frame = self.storage.fetch_sql("SELECT * FROM collector_state WHERE collector_name = 'orderbook_recorder' ORDER BY id DESC LIMIT 1")
        state = state_frame.iloc[0].to_dict() if not state_frame.empty else {}
        now = datetime.now(timezone.utc)
        parsed_last = _parse_ts(last_ts)
        seconds_since_last = (now - parsed_last).total_seconds() if parsed_last else None
        expected_interval = settings.orderbook_record_interval_seconds
        state_updated = _parse_ts(state.get("updated_at")) if state else None
        heartbeat_age = (now - state_updated).total_seconds() if state_updated else None
        process_appears_stale = bool(
            seconds_since_last is None
            or seconds_since_last > max(expected_interval * 2, 120)
        )
        status = "BROKEN_NO_RECENT_SNAPSHOTS"
        recommendation = "Recorder appears stuck. Restart with pure orderbook mode: python main.py record-orderbooks --weather-only --interval-seconds 30"
        if parsed_last:
            if now - parsed_last < timedelta(minutes=5):
                status = "HEALTHY"
                recommendation = "HEALTHY: orderbook snapshots are recent."
            elif now - parsed_last < timedelta(minutes=30):
                status = "DEGRADED_BUT_COLLECTING"
                recommendation = "DEGRADED_BUT_COLLECTING: snapshots are recent enough, but check the recorder window."
        missing_intervals = []
        if not frame.empty:
            by_hour = pd.to_datetime(frame["ts"]).dt.floor("h").value_counts().sort_index()
            missing_intervals = [str(idx) for idx, count in by_hour.items() if int(count) == 0]
        return CommandResult(
            {
                "last_hours": last_hours,
                "snapshots_collected": int(len(frame)),
                "unique_markets": int(frame["market_ticker"].nunique()) if not frame.empty else 0,
                "snapshots_per_hour": int(len(frame) / max(last_hours, 1)),
                "last_snapshot_time": last_ts,
                "seconds_since_last_snapshot": seconds_since_last,
                "expected_interval_seconds": expected_interval,
                "process_appears_stale": process_appears_stale,
                "collector_state": {
                    "status": state.get("status"),
                    "current_task": state.get("current_task"),
                    "last_heartbeat_at": state.get("last_heartbeat_at"),
                    "heartbeat_age_seconds": heartbeat_age,
                    "cycles_completed": state.get("cycles_completed"),
                    "snapshots_this_run": state.get("snapshots_this_run"),
                    "markets_tracked": state.get("markets_tracked"),
                    "error_message": state.get("error_message"),
                },
                "missing_intervals": missing_intervals[:20],
                "kalshi_429_count": "not persisted; check current process logs",
                "nws_timeout_count": "not persisted; check current process logs",
                "settlement_skipped_count": len(self.diagnose_settlement_skips(last_days=7).payload.get("top_skipped", [])),
                "recommendation": recommendation,
                "health": status,
            }
        )

    def weather_recorder_health(self, last_hours: int = 24) -> CommandResult:
        self.storage.init_db()
        since = (datetime.now(timezone.utc) - timedelta(hours=last_hours)).strftime("%Y-%m-%d %H:%M:%S")
        obs = self.storage.fetch_sql("SELECT * FROM weather_observation_snapshots_live WHERE ts_recorded >= :since", {"since": since})
        forecasts = self.storage.fetch_sql("SELECT * FROM weather_forecast_snapshots_live WHERE ts_recorded >= :since", {"since": since})
        station_map = self.storage.fetch_table("active_weather_station_map", limit=100000)
        obs_last = _last_by_station(obs, "ts_recorded")
        fcst_last = _last_by_station(forecasts, "ts_recorded")
        mapped_stations = set(station_map.get("station_code", pd.Series(dtype=object)).dropna().astype(str)) if not station_map.empty else set()
        observed_stations = set(obs.get("station_code", pd.Series(dtype=object)).dropna().astype(str)) if not obs.empty else set()
        forecasted_stations = set(forecasts.get("station_code", pd.Series(dtype=object)).dropna().astype(str)) if not forecasts.empty else set()
        low_conf = []
        if not station_map.empty and "mapping_confidence" in station_map:
            low_conf = station_map[pd.to_numeric(station_map["mapping_confidence"], errors="coerce").fillna(0) < 0.75][["market_ticker", "city", "station_code", "mapping_confidence", "warnings"]].head(50).to_dict("records")
        health = "HEALTHY"
        recommendation = "HEALTHY: observations and forecasts are recording."
        if obs.empty and forecasts.empty:
            health = "BROKEN_NO_RECENT_WEATHER"
            recommendation = "No recent weather rows. Start: python main.py record-weather-observations --from-active-markets --interval-minutes 5"
        elif obs.empty:
            health = "BROKEN_NO_RECENT_WEATHER"
            recommendation = "Observations are not recording. Start record-weather-observations in a separate terminal."
        elif forecasts.empty:
            health = "FORECASTS_NOT_RECORDING"
            recommendation = "Forecasts are not recording. Start record-weather-forecasts in a separate terminal."
        elif mapped_stations and (mapped_stations - observed_stations):
            health = "DEGRADED_BUT_COLLECTING"
            recommendation = "Some mapped stations have no recent observations; check station mappings and NWS availability."
        if low_conf:
            health = "MISSING_STATION_MAPPING" if health == "HEALTHY" else health
        return CommandResult(
            {
                "last_hours": last_hours,
                "observation_rows_collected": int(len(obs)),
                "forecast_rows_collected": int(len(forecasts)),
                "unique_stations_observed": int(len(observed_stations)),
                "unique_stations_forecasted": int(len(forecasted_stations)),
                "last_observation_timestamp_by_station": obs_last,
                "last_forecast_timestamp_by_station": fcst_last,
                "stations_missing_observations": sorted(mapped_stations - observed_stations),
                "stations_missing_forecasts": sorted(mapped_stations - forecasted_stations),
                "station_mapping_confidence_issues": low_conf,
                "nws_timeout_or_error_counts": "not persisted by weather recorder; check current recorder process logs",
                "recommendation": recommendation,
                "health": health,
            }
        )

    def rebuild_clean_edge_analysis(self, last_days: int = 3, dry_run: bool = False) -> CommandResult:
        commands = [
            f"python main.py reparse-contracts --weather-only --parser-version {PARSER_VERSION}",
            f"python main.py rebuild-settlement-labels --weather-only --settlement-version {SETTLEMENT_VERSION}",
            "python main.py validate-settlement-labels --weather-only",
            "python main.py validate-settlement-sources",
            f"python main.py mark-stale-runs --before-parser-version {PARSER_VERSION}",
            f"python main.py build-recorded-replay --last-days {last_days} --min-settlement-confidence 0.85",
            f"python main.py sweep-recorded --last-days {last_days}",
            f"python main.py edge-report --last-days {last_days}",
        ]
        if dry_run:
            return CommandResult({"dry_run": True, "commands": commands})
        results = {
            "reparse": self.reparse_contracts(weather_only=True).payload,
            "settlements": self.rebuild_settlement_labels().payload,
            "validate_labels": self.validate_settlement_labels(weather_only=True).payload,
            "validate_sources": self.validate_settlement_sources(fix=True).payload,
            "mark_stale": self.mark_stale_runs().payload,
        }
        replay = RecordedOrderbookReplayBuilder(storage=self.storage).build(last_days=last_days, min_settlement_confidence=0.85)
        sweep = RecordedOrderbookBacktester(storage=self.storage).sweep(last_days=last_days, label_quality="primary")
        report = EdgeReportGenerator(storage=self.storage).generate(last_days=last_days)
        results.update({"replay": replay.to_dict(), "sweep_recommendation": sweep.get("recommendation"), "report": report.to_dict()})
        return CommandResult(results)

    def daily_trading_research_update(self, last_days: int = 7) -> CommandResult:
        reports = PROJECT_ROOT / "reports"
        reports.mkdir(exist_ok=True)
        results: dict[str, Any] = {}
        results["collector_health"] = self.collector_health(last_hours=24).payload
        results["weather_recorder_health"] = self.weather_recorder_health(last_hours=24).payload
        try:
            from backtest.recorded_audit import RecordedDataAuditor

            audit = RecordedDataAuditor(storage=self.storage).audit(persist=True)
            results["audit_recorded_data"] = audit.to_dict()
        except Exception as exc:
            results["audit_recorded_data"] = {"error": str(exc)}
        results["settlement_sources"] = self.validate_settlement_sources(fix=True).payload
        results["orderbook_depths"] = self.validate_orderbook_depths(last_days=last_days).payload
        try:
            replay = RecordedOrderbookReplayBuilder(storage=self.storage).build(last_days=last_days, min_settlement_confidence=0.85)
            results["build_recorded_replay"] = replay.to_dict()
        except Exception as exc:
            results["build_recorded_replay"] = {"error": str(exc)}
        results["validate_signals"] = SignalValidator(self.storage).validate(last_days=last_days).to_dict()
        results["liquidity"] = LiquidityAnalyzer(self.storage).analyze(last_days=last_days, persist_exports=True).to_dict()
        try:
            results["sweep_recorded"] = RecordedOrderbookBacktester(storage=self.storage).sweep(last_days=last_days, label_quality="primary")
        except Exception as exc:
            results["sweep_recorded"] = {"error": str(exc)}
        results["trading_readiness"] = TradingReadiness(self.storage).evaluate(last_days=last_days).to_dict()
        try:
            report = EdgeReportGenerator(storage=self.storage).generate(last_days=last_days)
            results["edge_report"] = report.to_dict()
        except Exception as exc:
            results["edge_report"] = {"error": str(exc)}
        self._export_daily_manual_review_placeholders()
        path = reports / f"daily_trading_research_update_{datetime.now().strftime('%Y%m%d')}.md"
        path.write_text(_daily_report_markdown(results), encoding="utf-8")
        results["report_path"] = str(path)
        return CommandResult(results)

    def _latest_contracts(self, last_days: int | None = None) -> pd.DataFrame:
        frame = self.storage.fetch_table("parsed_contracts", limit=500000)
        if frame.empty:
            return frame
        rows = []
        seen: set[str] = set()
        cutoff = date.today() - timedelta(days=max(last_days or 0, 1)) if last_days else None
        for _, row in frame.sort_values("id", ascending=False).iterrows():
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            ticker = str(payload.get("market_ticker") or "")
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            local_date = payload.get("local_date")
            if cutoff and local_date:
                try:
                    if date.fromisoformat(str(local_date)) < cutoff:
                        continue
                except ValueError:
                    pass
            rows.append(payload)
        return pd.DataFrame(rows)

    def _settlement_yes_results(self) -> dict[str, int | None]:
        labels = self.storage.fetch_table("settlement_labels", limit=500000)
        if labels.empty:
            return {}
        return {str(row["market_ticker"]): (None if pd.isna(row.get("yes_result")) else int(row["yes_result"])) for _, row in labels.iterrows()}

    def _export_changed_labels(self, changed: list[dict[str, Any]]) -> None:
        reports = PROJECT_ROOT / "reports"
        reports.mkdir(exist_ok=True)
        pd.DataFrame(changed).to_csv(reports / "manual_review_changed_labels.csv", index=False)

    def _export_daily_manual_review_placeholders(self) -> None:
        reports = PROJECT_ROOT / "reports"
        reports.mkdir(exist_ok=True)
        for name in ["paper_ready_candidates.csv", "fair_value_candidates.csv", "skipped_due_to_data_quality.csv"]:
            path = reports / name
            if not path.exists():
                pd.DataFrame().to_csv(path, index=False)

    def _count(self, table_name: str) -> int:
        frame = self.storage.fetch_sql(f"SELECT COUNT(*) AS count FROM {table_name}")
        return int(frame.iloc[0]["count"]) if not frame.empty else 0

    def _count_where(self, table_name: str, clause: str) -> int:
        frame = self.storage.fetch_sql(f"SELECT COUNT(*) AS count FROM {table_name} WHERE {clause}")
        return int(frame.iloc[0]["count"]) if not frame.empty else 0

    def _latest_value(self, table_name: str, column: str) -> Any:
        frame = self.storage.fetch_sql(f"SELECT {column} FROM {table_name} WHERE {column} IS NOT NULL ORDER BY id DESC LIMIT 1")
        return None if frame.empty else frame.iloc[0][column]

    def _latest_sweep_recommendation(self) -> str | None:
        frame = self.storage.fetch_sql("SELECT recommendation FROM recorded_strategy_sweeps WHERE COALESCE(is_stale, 0) = 0 ORDER BY id DESC LIMIT 1")
        return None if frame.empty else str(frame.iloc[0]["recommendation"])


def _key_tables() -> list[str]:
    return [
        "markets",
        "parsed_contracts",
        "settlement_labels",
        "nws_daily_climate_reports",
        "orderbook_snapshots_live",
        "active_weather_station_map",
        "weather_observation_snapshots_live",
        "weather_forecast_snapshots_live",
        "recorded_orderbook_replay_snapshots",
        "signals",
        "backtest_runs",
        "backtest_trades",
        "recorded_data_audits",
        "recorded_strategy_sweeps",
        "collector_state",
    ]


def _skip_reason(row) -> str:
    if not row.get("station_code"):
        return "station_mapping_missing"
    if row.get("contract_type") == "unknown":
        return "parser_unknown_contract_type"
    if row.get("variable_type") not in {"high_temp", "low_temp"}:
        return "unsupported_variable_type"
    if not row.get("local_date"):
        return "settlement_value_missing"
    try:
        if date.fromisoformat(str(row.get("local_date"))) >= date.today():
            return "date_not_resolved_yet"
    except ValueError:
        return "settlement_value_missing"
    return "nws_cli_report_not_found_or_hourly_observations_missing"


def _suggested_skip_fixes(skipped: pd.DataFrame) -> list[str]:
    reasons = skipped["reason"].value_counts().to_dict()
    fixes = []
    if reasons.get("station_mapping_missing"):
        fixes.append("Add or validate station mappings for the skipped cities.")
    if reasons.get("parser_unknown_contract_type"):
        fixes.append("Inspect skipped market titles/rules and extend parser tests.")
    if reasons.get("nws_cli_report_not_found_or_hourly_observations_missing"):
        fixes.append("Debug NWS CLI report lookup and IEM/ASOS fallback for the top skipped stations.")
    return fixes or ["No obvious single fix; inspect top skipped tickers manually."]


def _json_snippet(value: Any) -> Any:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return str(value)[:200]
    return parsed[:3] if isinstance(parsed, list) else parsed


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


def _last_by_station(frame: pd.DataFrame, column: str) -> dict[str, str]:
    if frame.empty or "station_code" not in frame or column not in frame:
        return {}
    values = frame.dropna(subset=["station_code", column]).copy()
    if values.empty:
        return {}
    values[column] = pd.to_datetime(values[column], errors="coerce")
    grouped = values.sort_values(column).groupby("station_code").tail(1)
    return {str(row["station_code"]): str(row[column]) for _, row in grouped.iterrows()}


def _daily_report_markdown(results: dict[str, Any]) -> str:
    readiness = results.get("trading_readiness", {})
    liquidity = results.get("liquidity", {}).get("summary", {})
    signal_summary = results.get("validate_signals", {}).get("summary_by_strategy", [])
    lines = [
        "# Daily Trading Research Update",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Executive Summary",
        "",
        f"- Trading readiness: {readiness.get('status')}",
        f"- Message: {readiness.get('message')}",
        f"- Passive liquidity verdict: {liquidity.get('passive_verdict')}",
        "- Live trading justified: no",
        "",
        "## Signal Validation",
        "",
    ]
    if signal_summary:
        for row in signal_summary:
            lines.append(f"- {row.get('strategy')}: signals={row.get('signal_count')} beat_30m={row.get('beat_30m_pct')} avg_future_edge={row.get('average_future_price_edge_cents')}")
    else:
        lines.append("- No validated signal rows.")
    lines.extend(
        [
            "",
            "## Data / Health",
            "",
            "```json",
            json.dumps(
                {
                    "collector_health": results.get("collector_health"),
                    "weather_recorder_health": results.get("weather_recorder_health"),
                    "audit": results.get("audit_recorded_data"),
                },
                indent=2,
                default=str,
            ),
            "```",
            "",
            "## What To Do Tomorrow",
            "",
            f"- Next command: {readiness.get('next_command')}",
            "- Keep orderbook/weather recorders running if data gaps remain.",
            "- Do not trade real money.",
        ]
    )
    return "\n".join(lines)
