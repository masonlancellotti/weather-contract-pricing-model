from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import date

from backtest.edge_report import EdgeReportGenerator
from backtest.recorded_audit import RecordedDataAuditor
from backtest.recorded_backtester import RecordedOrderbookBacktester
from backtest.recorded_replay import RecordedOrderbookReplayBuilder
from backtest.replay_builder import ReplayBuilder
from backtest.runner import BacktestRunner
from config import settings
from data.active_weather_station_resolver import ActiveWeatherStationResolver
from data.nws_climate_report_client import NWSClimateReportClient
from data.kalshi_historical_loader import KalshiHistoricalLoader
from data.kalshi_market_loader import KalshiMarketLoader
from data.storage import Storage
from data.weather_settlement_loader import WeatherSettlementLoader
from live.collector import LiveDataCollector
from live.orderbook_recorder import LiveOrderbookRecorder
from live.weather_recorder import WeatherForecastRecorder, WeatherObservationRecorder
from live.paper_trader import PaperTrader
from live.scanner import LiveScanner
from maintenance import ProjectMaintenance
from research.liquidity_analysis import LiquidityAnalyzer
from research.opportunity_ranker import OpportunityRanker
from research.signal_validation import SignalValidator
from research.trading_readiness import TradingReadiness


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="Kalshi-only weather edge research system")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create local SQLite schema")
    load = sub.add_parser("load-markets", help="Load active Kalshi weather markets")
    load.add_argument("--max-pages", type=int, default=None)
    load.add_argument("--max-series", type=int, default=None)
    scan = sub.add_parser("scan-live", help="Run one live scanner cycle in paper-only mode")
    scan.add_argument("--max-markets", type=int, default=50)
    backtest = sub.add_parser("backtest", help="Run conservative backtest from local replay data")
    backtest.add_argument("--strategy", required=False)
    backtest.add_argument("--all", action="store_true")
    backtest.add_argument("--start", required=False)
    backtest.add_argument("--end", required=False)
    backtest.add_argument("--mode", choices=["taker", "signal", "passive"], default="taker")
    backtest.add_argument("--label-quality", choices=["primary", "exploratory", "all"], default="primary")
    history = sub.add_parser("load-history", help="Load settled weather markets plus historical Kalshi candlesticks/trades")
    history.add_argument("--start")
    history.add_argument("--end")
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--market-ticker")
    history.add_argument("--weather-only", action="store_true")
    history.add_argument("--period", type=int, default=60)
    history.add_argument("--no-trades", action="store_true")
    settlements = sub.add_parser("build-settlements", help="Build weather settlement labels from station observations")
    settlements.add_argument("--start")
    settlements.add_argument("--end")
    settlements.add_argument("--market-ticker")
    exact = sub.add_parser("build-exact-settlements", help="Build settlement labels, preferring exact NWS CLI reports when available")
    exact.add_argument("--start")
    exact.add_argument("--end")
    exact.add_argument("--market-ticker")
    climate = sub.add_parser("fetch-nws-climate-report", help="Fetch and parse one NWS Daily Climate Report")
    climate.add_argument("--station", required=True)
    climate.add_argument("--date", required=True)
    replay = sub.add_parser("build-replay", help="Build no-lookahead replay snapshots")
    replay.add_argument("--start")
    replay.add_argument("--end")
    replay.add_argument("--market-ticker")
    live_replay = sub.add_parser("build-live-orderbook-replay", help="Build replay snapshots from locally recorded full orderbooks")
    live_replay.add_argument("--start")
    live_replay.add_argument("--end")
    live_replay.add_argument("--market-ticker")
    sub.add_parser("audit-recorded-data", help="Audit locally recorded full orderbook data")
    recorded_replay = sub.add_parser("build-recorded-replay", help="Build no-lookahead replay rows from recorded full orderbooks")
    recorded_replay.add_argument("--start")
    recorded_replay.add_argument("--end")
    recorded_replay.add_argument("--last-days", type=int)
    recorded_replay.add_argument("--market-ticker")
    recorded_replay.add_argument("--min-settlement-confidence", type=float, default=0.85)
    recorded_replay.add_argument("--allow-unsettled", action="store_true")
    recorded_replay.add_argument("--store-depth-json", action="store_true", help="Duplicate full depth JSON into replay rows. Off by default to avoid bloating SQLite.")
    recorded_backtest = sub.add_parser("backtest-recorded", help="Backtest using recorded full-orderbook replay rows")
    recorded_backtest.add_argument("--strategy")
    recorded_backtest.add_argument("--all", action="store_true")
    recorded_backtest.add_argument("--start")
    recorded_backtest.add_argument("--end")
    recorded_backtest.add_argument("--last-days", type=int)
    recorded_backtest.add_argument("--mode", choices=["default", "taker", "signal_only", "conservative_passive", "full_orderbook_passive_approx"], default="default")
    recorded_backtest.add_argument("--label-quality", choices=["primary", "exploratory", "all"], default="primary")
    sweep = sub.add_parser("sweep-recorded", help="Run recorded-orderbook strategy parameter sweeps")
    sweep.add_argument("--start")
    sweep.add_argument("--end")
    sweep.add_argument("--last-days", type=int)
    sweep.add_argument("--label-quality", choices=["primary", "exploratory", "all"], default="primary")
    report = sub.add_parser("edge-report", help="Generate markdown edge report from recorded data")
    report.add_argument("--start")
    report.add_argument("--end")
    report.add_argument("--last-days", type=int, default=3)
    recorder = sub.add_parser("record-orderbooks", help="Record current full Kalshi orderbooks; places no orders")
    recorder.add_argument("--market-ticker")
    recorder.add_argument("--weather-only", action="store_true")
    recorder.add_argument("--interval-seconds", type=int, default=None)
    recorder.add_argument("--duration-minutes", type=int, default=None)
    recorder.add_argument("--duration-hours", type=float, default=None)
    recorder.add_argument("--max-markets", type=int, default=None)
    recorder.add_argument("--top-depth-only", action="store_true")
    recorder.add_argument("--verbose-orderbooks", action="store_true")
    recorder.add_argument("--once", action="store_true", help="Record one cycle and exit. Mainly useful for smoke tests.")
    station_resolve = sub.add_parser("resolve-active-weather-stations", help="Map active Kalshi weather markets to likely settlement stations")
    station_resolve.add_argument("--max-markets", type=int, default=None)
    station_resolve.add_argument("--all-markets", action="store_true")
    obs_recorder = sub.add_parser("record-weather-observations", help="Record live station observations in a separate process")
    obs_recorder.add_argument("--weather-only", action="store_true")
    obs_recorder.add_argument("--from-active-markets", action="store_true")
    obs_recorder.add_argument("--stations")
    obs_recorder.add_argument("--interval-minutes", type=int, default=5)
    obs_recorder.add_argument("--duration-hours", type=float, default=None)
    obs_recorder.add_argument("--max-markets", type=int, default=None)
    obs_recorder.add_argument("--once", action="store_true")
    fcst_recorder = sub.add_parser("record-weather-forecasts", help="Record live forecast snapshots in a separate process")
    fcst_recorder.add_argument("--weather-only", action="store_true")
    fcst_recorder.add_argument("--from-active-markets", action="store_true")
    fcst_recorder.add_argument("--stations")
    fcst_recorder.add_argument("--interval-minutes", type=int, default=30)
    fcst_recorder.add_argument("--duration-hours", type=float, default=None)
    fcst_recorder.add_argument("--max-markets", type=int, default=None)
    fcst_recorder.add_argument("--once", action="store_true")
    collector = sub.add_parser("collect-live", help="Run the no-trading live data collector for hours/days")
    collector.add_argument("--duration-hours", type=float, default=settings.collector_default_duration_hours, help="How long to run. Use 0 for indefinite.")
    collector.add_argument("--interval-seconds", type=int, default=None, help="Orderbook recording interval.")
    collector.add_argument("--max-markets", type=int, default=None)
    collector.add_argument("--scan-interval-minutes", type=int, default=settings.collector_scan_interval_minutes)
    collector.add_argument("--maintenance-interval-minutes", type=int, default=settings.collector_maintenance_interval_minutes)
    collector.add_argument("--settlement-lookback-days", type=int, default=settings.collector_settlement_lookback_days)
    collector.add_argument("--all-markets", action="store_true", help="Record all active markets instead of weather-filtered markets.")
    sub.add_parser("project-status", help="Print durable handoff/project status")
    reparse = sub.add_parser("reparse-contracts", help="Reparse stored markets with current parser semantics")
    reparse.add_argument("--weather-only", action="store_true")
    reparse.add_argument("--parser-version", default=None)
    rebuild_labels = sub.add_parser("rebuild-settlement-labels", help="Rebuild settlement labels with current settlement semantics")
    rebuild_labels.add_argument("--start")
    rebuild_labels.add_argument("--end")
    rebuild_labels.add_argument("--market-ticker")
    rebuild_labels.add_argument("--weather-only", action="store_true")
    rebuild_labels.add_argument("--settlement-version", default=None)
    validate_labels = sub.add_parser("validate-settlement-labels", help="Validate settlement labels and semantic fields")
    validate_labels.add_argument("--weather-only", action="store_true")
    validate_sources = sub.add_parser("validate-settlement-sources", help="Validate/fix settlement source metadata")
    validate_sources.add_argument("--no-fix", action="store_true")
    stale = sub.add_parser("mark-stale-runs", help="Mark old parser/settlement runs stale")
    stale.add_argument("--before-parser-version", default=None)
    clean = sub.add_parser("rebuild-clean-edge-analysis", help="Run the v2 semantic cleanup pipeline")
    clean.add_argument("--last-days", type=int, default=3)
    clean.add_argument("--dry-run", action="store_true")
    skip_diag = sub.add_parser("diagnose-settlement-skips", help="Group skipped settlements by root cause")
    skip_diag.add_argument("--last-days", type=int, default=7)
    city_diag = sub.add_parser("debug-city-settlements", help="Debug settlement labels for a city/prefix")
    city_diag.add_argument("--city", required=True)
    city_diag.add_argument("--last-days", type=int, default=10)
    depth_diag = sub.add_parser("validate-orderbook-depths", help="Inspect extreme recorded orderbook depths")
    depth_diag.add_argument("--last-days", type=int, default=3)
    health = sub.add_parser("collector-health", help="Summarize live collector health from DB")
    health.add_argument("--last-hours", type=int, default=24)
    weather_health = sub.add_parser("weather-recorder-health", help="Summarize live weather/forecast recorder health from DB")
    weather_health.add_argument("--last-hours", type=int, default=24)
    readiness = sub.add_parser("trading-readiness", help="Score whether research is ready for paper/live trading")
    readiness.add_argument("--last-days", type=int, default=7)
    liquidity = sub.add_parser("analyze-liquidity", help="Analyze spread persistence, passive fill evidence, and adverse selection")
    liquidity.add_argument("--last-days", type=int, default=7)
    rank = sub.add_parser("rank-opportunities", help="Rank current active markets by executable fair-value edge")
    rank.add_argument("--weather-only", action="store_true")
    rank.add_argument("--max-markets", type=int, default=100)
    validate_signals = sub.add_parser("validate-signals", help="Validate signals/trades against future recorded prices")
    validate_signals.add_argument("--last-days", type=int, default=7)
    daily = sub.add_parser("daily-trading-research-update", help="Run daily research update and save markdown report")
    daily.add_argument("--last-days", type=int, default=7)
    sub.add_parser("dashboard", help="Start Streamlit dashboard")
    paper = sub.add_parser("paper-trade", help="Run paper-only opportunity simulation; never sends real orders")
    paper.add_argument("--strategy", default="rank_opportunities")
    paper.add_argument("--weather-only", action="store_true")
    paper.add_argument("--mode", choices=["taker_paper", "passive_paper_conservative"], default="taker_paper")
    paper.add_argument("--max-markets", type=int, default=100)

    args = parser.parse_args(argv)
    if args.command == "init-db":
        Storage().init_db()
        print("Initialized SQLite schema.")
        return 0
    if args.command == "load-markets":
        markets = KalshiMarketLoader().load_active_weather_markets(max_pages=args.max_pages, max_series=args.max_series)
        print(f"Loaded {len(markets)} likely weather markets.")
        return 0
    if args.command == "scan-live":
        rows = LiveScanner().scan_once(max_markets=args.max_markets)
        for row in rows:
            print(f"{row.get('action')} {row.get('market_ticker')}: {row.get('reason')}")
        return 0
    if args.command == "load-history":
        result = KalshiHistoricalLoader().load_history(
            start=_date_arg(args.start),
            end=_date_arg(args.end),
            limit=args.limit,
            market_ticker=args.market_ticker,
            weather_only=args.weather_only or not args.market_ticker,
            period_interval=args.period,
            include_trades=not args.no_trades,
        )
        print(result.to_dict())
        return 0
    if args.command == "build-settlements":
        result = WeatherSettlementLoader().build_settlements(start=_date_arg(args.start), end=_date_arg(args.end), market_ticker=args.market_ticker)
        print(result.to_dict())
        return 0
    if args.command == "build-exact-settlements":
        result = WeatherSettlementLoader().build_settlements(start=_date_arg(args.start), end=_date_arg(args.end), market_ticker=args.market_ticker)
        print(result.to_dict())
        return 0
    if args.command == "fetch-nws-climate-report":
        result = NWSClimateReportClient().fetch_report(args.station, _date_arg(args.date), persist=True)
        print(result.to_storage_row())
        return 0
    if args.command == "build-replay":
        result = ReplayBuilder().build(start=_date_arg(args.start), end=_date_arg(args.end), market_ticker=args.market_ticker)
        print(result.to_dict())
        return 0
    if args.command == "build-live-orderbook-replay":
        result = ReplayBuilder().build_from_live_orderbooks(start=_date_arg(args.start), end=_date_arg(args.end), market_ticker=args.market_ticker)
        print(result.to_dict())
        return 0
    if args.command == "audit-recorded-data":
        result = RecordedDataAuditor().audit(persist=True)
        print(result.to_text())
        return 0
    if args.command == "build-recorded-replay":
        result = RecordedOrderbookReplayBuilder().build(
            start=_date_arg(args.start),
            end=_date_arg(args.end),
            market_ticker=args.market_ticker,
            last_days=args.last_days,
            min_settlement_confidence=args.min_settlement_confidence,
            allow_unsettled=args.allow_unsettled,
            store_depth_json=args.store_depth_json,
        )
        print(result.to_dict())
        return 0
    if args.command == "backtest-recorded":
        result = RecordedOrderbookBacktester().run(
            "all" if args.all else (args.strategy or "already_hit"),
            start=_date_arg(args.start),
            end=_date_arg(args.end),
            last_days=args.last_days,
            mode=args.mode,
            label_quality=args.label_quality,
        )
        print_recorded_result(result)
        return 0
    if args.command == "sweep-recorded":
        result = RecordedOrderbookBacktester().sweep(
            start=_date_arg(args.start),
            end=_date_arg(args.end),
            last_days=args.last_days,
            label_quality=args.label_quality,
        )
        print_sweep_result(result)
        return 0
    if args.command == "edge-report":
        result = EdgeReportGenerator().generate(
            start=_date_arg(args.start),
            end=_date_arg(args.end),
            last_days=args.last_days,
        )
        print(result.to_dict())
        return 0
    if args.command == "record-orderbooks":
        result = LiveOrderbookRecorder().run(
            market_ticker=args.market_ticker,
            weather_only=args.weather_only or not args.market_ticker,
            interval_seconds=args.interval_seconds,
            duration_minutes=args.duration_minutes,
            duration_hours=args.duration_hours,
            max_markets=args.max_markets,
            full_depth=not args.top_depth_only,
            verbose_orderbooks=args.verbose_orderbooks,
            once=args.once,
        )
        print(result.to_dict())
        return 0
    if args.command == "resolve-active-weather-stations":
        result = ActiveWeatherStationResolver().resolve_active(weather_only=not args.all_markets, max_markets=args.max_markets, persist=True)
        print(result.to_text())
        return 0
    if args.command == "record-weather-observations":
        result = WeatherObservationRecorder().run(
            stations=_station_list_arg(args.stations),
            from_active_markets=args.from_active_markets or args.weather_only or not args.stations,
            weather_only=args.weather_only or not args.stations,
            interval_minutes=args.interval_minutes,
            duration_hours=args.duration_hours,
            max_markets=args.max_markets,
            once=args.once,
        )
        print(result.to_dict())
        return 0
    if args.command == "record-weather-forecasts":
        result = WeatherForecastRecorder().run(
            stations=_station_list_arg(args.stations),
            from_active_markets=args.from_active_markets or args.weather_only or not args.stations,
            weather_only=args.weather_only or not args.stations,
            interval_minutes=args.interval_minutes,
            duration_hours=args.duration_hours,
            max_markets=args.max_markets,
            once=args.once,
        )
        print(result.to_dict())
        return 0
    if args.command == "collect-live":
        result = LiveDataCollector().run(
            duration_hours=args.duration_hours,
            interval_seconds=args.interval_seconds,
            max_markets=args.max_markets,
            scan_interval_minutes=args.scan_interval_minutes,
            maintenance_interval_minutes=args.maintenance_interval_minutes,
            settlement_lookback_days=args.settlement_lookback_days,
            weather_only=not args.all_markets,
        )
        print(result.to_dict())
        return 0
    if args.command == "paper-trade":
        result = PaperTrader().run_once(strategy=args.strategy, weather_only=args.weather_only or True, mode=args.mode, max_markets=args.max_markets)
        print(result)
        return 0
    if args.command == "project-status":
        print(ProjectMaintenance().project_status().to_text())
        return 0
    if args.command == "reparse-contracts":
        print(ProjectMaintenance().reparse_contracts(weather_only=args.weather_only).to_text())
        return 0
    if args.command == "rebuild-settlement-labels":
        print(ProjectMaintenance().rebuild_settlement_labels(start=_date_arg(args.start), end=_date_arg(args.end), market_ticker=args.market_ticker).to_text())
        return 0
    if args.command == "validate-settlement-labels":
        print(ProjectMaintenance().validate_settlement_labels(weather_only=args.weather_only).to_text())
        return 0
    if args.command == "validate-settlement-sources":
        print(ProjectMaintenance().validate_settlement_sources(fix=not args.no_fix).to_text())
        return 0
    if args.command == "mark-stale-runs":
        print(ProjectMaintenance().mark_stale_runs().to_text())
        return 0
    if args.command == "rebuild-clean-edge-analysis":
        print(ProjectMaintenance().rebuild_clean_edge_analysis(last_days=args.last_days, dry_run=args.dry_run).to_text())
        return 0
    if args.command == "diagnose-settlement-skips":
        print(ProjectMaintenance().diagnose_settlement_skips(last_days=args.last_days).to_text())
        return 0
    if args.command == "debug-city-settlements":
        print(ProjectMaintenance().debug_city_settlements(city=args.city, last_days=args.last_days).to_text())
        return 0
    if args.command == "validate-orderbook-depths":
        print(ProjectMaintenance().validate_orderbook_depths(last_days=args.last_days).to_text())
        return 0
    if args.command == "collector-health":
        print(ProjectMaintenance().collector_health(last_hours=args.last_hours).to_text())
        return 0
    if args.command == "weather-recorder-health":
        print(ProjectMaintenance().weather_recorder_health(last_hours=args.last_hours).to_text())
        return 0
    if args.command == "trading-readiness":
        print(TradingReadiness().evaluate(last_days=args.last_days).to_text())
        return 0
    if args.command == "analyze-liquidity":
        print(LiquidityAnalyzer().analyze(last_days=args.last_days).to_text())
        return 0
    if args.command == "rank-opportunities":
        print(OpportunityRanker().rank(weather_only=args.weather_only or True, max_markets=args.max_markets).to_text())
        return 0
    if args.command == "validate-signals":
        print(SignalValidator().validate(last_days=args.last_days).to_text())
        return 0
    if args.command == "daily-trading-research-update":
        print(ProjectMaintenance().daily_trading_research_update(last_days=args.last_days).to_text())
        return 0
    if args.command == "backtest":
        result = BacktestRunner().run(
            "all" if args.all else (args.strategy or "late_day_high_fade"),
            start=_date_arg(args.start),
            end=_date_arg(args.end),
            mode=args.mode,
            label_quality=args.label_quality,
        )
        if "runs" in result:
            for item in result["runs"]:
                print_backtest_result(item)
        else:
            print_backtest_result(result)
        return 0
    if args.command == "dashboard":
        return subprocess.call([sys.executable, "-m", "streamlit", "run", "dashboard/app.py"])
    return 1


def _date_arg(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _station_list_arg(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def print_backtest_result(result: dict) -> None:
    if "message" in result:
        print(f"run_id={result.get('run_id')} strategy={result.get('strategy')} mode={result.get('mode')}")
        print(f"markets={result.get('markets')} snapshots={result.get('replay_snapshots')} signals={result.get('signals')} filled={result.get('filled_trades')}")
        print(f"excluded_low_confidence={result.get('markets_excluded_low_confidence')} label_sources={result.get('label_source_breakdown')}")
        print(f"replay_data_type={result.get('replay_data_type')} execution={result.get('execution_assumption')} label_quality={result.get('settlement_label_quality')}")
        print(f"gross_pnl={result.get('gross_pnl')} fees={result.get('fees')} net_pnl={result.get('net_pnl')} roi={result.get('roi')}")
        print(f"win_rate={result.get('win_rate')} max_drawdown={result.get('max_drawdown')} avg_edge={result.get('average_edge')}")
        print(result["message"])
        if result.get("limitations"):
            print("limitations:")
            for item in result["limitations"]:
                print(f"- {item}")
    else:
        print(result.get("summary") or result)


def print_recorded_result(result: dict) -> None:
    runs = result.get("runs")
    if runs:
        for item in runs:
            print_recorded_result(item)
        return
    summary = result.get("summary", result)
    print(f"strategy={summary.get('strategy')} mode={summary.get('mode')} label_quality={summary.get('label_quality')}")
    print(f"markets={summary.get('markets')} snapshots={summary.get('snapshots')} signals={summary.get('signals')} fills={summary.get('fills')}")
    print(f"gross_pnl={summary.get('gross_pnl')} fees={summary.get('fees')} net_pnl={summary.get('net_pnl')} roi={summary.get('roi')}")
    print(f"win_rate={summary.get('win_rate')} max_drawdown={summary.get('max_drawdown')} avg_edge={summary.get('average_edge_cents')}")
    print(f"execution={summary.get('execution_assumption')} data_quality={summary.get('data_quality_score')}")
    print(summary.get("message"))
    for item in summary.get("limitations", []):
        print(f"- {item}")
    for item in summary.get("warnings", [])[:10]:
        print(f"WARNING: {item}")


def print_sweep_result(result: dict) -> None:
    print(result.get("message"))
    print(f"strategy_variants_tested={result.get('strategy_variants_tested')} recommendation={result.get('recommendation')}")
    print("Top candidates:")
    for item in result.get("top_candidates", [])[:10]:
        print(
            f"- {item.get('strategy')} {item.get('params')} mode={item.get('mode')} "
            f"net_pnl={item.get('net_pnl'):.2f} fills={item.get('fills')} robustness={item.get('robustness_verdict')}"
        )
    print("Rejected strategies:")
    for item in result.get("rejected_strategies", [])[:10]:
        print(f"- {item.get('strategy')} {item.get('params')}: {item.get('reason')}")


if __name__ == "__main__":
    raise SystemExit(main())
