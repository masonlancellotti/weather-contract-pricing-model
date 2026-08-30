from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dashboard.dataframe_utils import flatten_json_columns, make_unique_columns
except ModuleNotFoundError:  # Streamlit may execute with dashboard/ as sys.path[0].
    from dataframe_utils import flatten_json_columns, make_unique_columns
from data.storage import Storage
from live.scanner import LiveScanner
from maintenance import ProjectMaintenance
from research.liquidity_analysis import LiquidityAnalyzer
from research.opportunity_ranker import OpportunityRanker
from research.signal_validation import SignalValidator
from research.trading_readiness import TradingReadiness


def safe_dataframe(frame: pd.DataFrame, **kwargs) -> None:
    st.dataframe(make_unique_columns(frame), width="stretch", **kwargs)


def _table_count(storage: Storage, table_name: str) -> int:
    return len(storage.fetch_table(table_name, limit=100000))


def _what_should_i_do_now(storage: Storage) -> str:
    try:
        live_books = _table_count(storage, "orderbook_snapshots_live")
        recorded_replay = _table_count(storage, "recorded_orderbook_replay_snapshots")
        exact_reports = _table_count(storage, "nws_daily_climate_reports")
        settlements = storage.fetch_table("settlement_labels", limit=100000)
        replay = _table_count(storage, "replay_snapshots")
        trades = storage.fetch_table("backtest_trades", limit=100000)
    except Exception as exc:
        return f"Dashboard data check failed: {exc}"
    if live_books == 0:
        return "What should I do now? Start recording live orderbooks: python main.py record-orderbooks --weather-only --interval-seconds 30"
    weather_obs = _table_count(storage, "weather_observation_snapshots_live")
    weather_fcst = _table_count(storage, "weather_forecast_snapshots_live")
    if weather_obs == 0:
        return "What should I do now? Start separate weather observations: python main.py record-weather-observations --from-active-markets --interval-minutes 5"
    if weather_fcst == 0:
        return "What should I do now? Start separate forecast snapshots: python main.py record-weather-forecasts --from-active-markets --interval-minutes 30"
    if recorded_replay == 0:
        return "What should I do now? Build recorded full-orderbook replay: python main.py build-recorded-replay --last-days 3"
    if exact_reports == 0:
        return "What should I do now? Build exact NWS settlement reports: python main.py build-exact-settlements --start YYYY-MM-DD --end YYYY-MM-DD"
    if settlements.empty or settlements["confidence"].fillna(0).max() < 0.85:
        return "What should I do now? Settlement labels are not high-confidence enough. Fetch exact reports or keep this exploratory."
    if replay == 0:
        return "What should I do now? Build replay snapshots after loading history and settlements."
    if trades.empty or len(trades) < 30:
        return "What should I do now? Keep collecting data. Do not trade live yet; sample size is too small."
    if trades["net_pnl"].fillna(0).sum() > 0:
        return "What should I do now? Candidate for tiny paper trading only. Keep real money disabled until robustness checks pass."
    return "What should I do now? No robust edge is proven. Keep collecting orderbooks and exact settlements."


st.set_page_config(page_title="Kalshi Weather Edge", layout="wide")
st.title("Kalshi Weather Edge")

storage = Storage()
storage.init_db()

st.info(_what_should_i_do_now(storage))

tabs = st.tabs(["Live Scanner", "Backtest Results", "Market Detail", "Risk", "Data Quality", "Historical Data", "Recorded Edge", "Trading Research"])

with tabs[0]:
    st.subheader("Live Scanner")
    max_markets = st.number_input("Max markets to scan", min_value=1, max_value=200, value=25)
    if st.button("Run one scan", type="primary"):
        with st.spinner("Scanning Kalshi and weather sources..."):
            rows = LiveScanner(storage=storage).scan_once(max_markets=int(max_markets))
        safe_dataframe(pd.DataFrame(rows))
    signals = storage.fetch_table("signals", limit=500)
    st.caption("Recent scanner decisions")
    safe_dataframe(flatten_json_columns(signals))

with tabs[1]:
    st.subheader("Backtest Results")
    runs = storage.fetch_table("backtest_runs", limit=200)
    if runs.empty:
        st.warning("No backtest runs yet. Run `python main.py backtest --strategy late_day_high_fade --start YYYY-MM-DD --end YYYY-MM-DD`.")
    else:
        show_stale = st.checkbox("Show stale/invalid runs", value=False, key="show_stale_backtest_runs")
        if "is_stale" in runs.columns and not show_stale:
            runs = runs[runs["is_stale"].fillna(0).astype(int).eq(0)]
        expanded = flatten_json_columns(runs)
        if not show_stale and expanded.empty:
            st.warning("Only stale/invalid runs exist. Rebuild clean edge analysis before trusting P&L.")
        safe_dataframe(expanded)
        trades = storage.fetch_table("backtest_trades", limit=5000)
        if not trades.empty:
            safe_dataframe(flatten_json_columns(trades))
        if not trades.empty and "net_pnl" in trades:
            trades = trades.sort_values("id")
            trades["cum_pnl_dollars"] = trades["net_pnl"].fillna(0).cumsum() / 100
            st.plotly_chart(px.line(trades, x="id", y="cum_pnl_dollars", title="Cumulative P&L"), width="stretch")

with tabs[2]:
    st.subheader("Market Detail")
    markets = storage.fetch_table("markets", limit=1000)
    if markets.empty:
        st.info("No markets loaded yet.")
    else:
        ticker = st.selectbox("Market", markets["ticker"].dropna().unique())
        row = markets[markets["ticker"] == ticker].iloc[0]
        st.json(row["payload"])
        contracts = storage.fetch_table("parsed_contracts", limit=5000)
        if not contracts.empty:
            safe_dataframe(flatten_json_columns(contracts[contracts["market_ticker"] == ticker]))

with tabs[3]:
    st.subheader("Risk")
    orders = storage.fetch_table("paper_orders", limit=1000)
    positions = storage.fetch_table("paper_positions", limit=1000)
    st.caption("Paper orders")
    safe_dataframe(flatten_json_columns(orders))
    st.caption("Paper positions")
    safe_dataframe(flatten_json_columns(positions))

with tabs[4]:
    st.subheader("Data Quality")
    contracts = storage.fetch_table("parsed_contracts", limit=5000)
    if contracts.empty:
        st.info("No parsed contracts yet.")
    else:
        expanded = flatten_json_columns(contracts)
        warning_cols = [col for col in expanded.columns if "warning" in col.lower()]
        st.metric("Parsed contracts", len(expanded))
        st.metric("Low parse confidence", int((expanded.get("parse_confidence", pd.Series(dtype=float)) < 0.75).sum()))
        safe_dataframe(expanded[["market_ticker", "event_ticker", "parse_confidence", *warning_cols]].head(1000))
    labels = storage.fetch_table("settlement_labels", limit=5000)
    if not labels.empty:
        st.caption("Settlement label source breakdown")
        exact_counts = labels.get("exact_source_type", pd.Series(dtype=object)).fillna(labels.get("source", pd.Series(dtype=object))).fillna("unknown").value_counts().reset_index()
        exact_counts.columns = ["source", "labels"]
        safe_dataframe(exact_counts)
        st.caption("Low-confidence settlement labels")
        safe_dataframe(labels[labels["confidence"].fillna(0) < 0.75].head(1000))
        if "exact_vs_fallback_diff" in labels:
            diffs = labels[labels["exact_vs_fallback_diff"].fillna(0).abs() > 0.01]
            st.caption("Exact-vs-fallback settlement differences")
            safe_dataframe(diffs.head(1000))
    if st.button("Diagnose settlement skips"):
        skip_result = ProjectMaintenance(storage).diagnose_settlement_skips(last_days=7).payload
        st.caption("Settlement Skips by Reason")
        safe_dataframe(pd.DataFrame([{"reason": key, "count": value} for key, value in skip_result.get("skipped_by_reason", {}).items()]))
    st.caption("Weather recorder health")
    weather_health = ProjectMaintenance(storage).weather_recorder_health(last_hours=24).payload
    st.json(weather_health)

with tabs[5]:
    st.subheader("Historical Data")
    counts = []
    for table_name in [
        "historical_candlesticks",
        "historical_trades",
        "settlement_labels",
        "replay_snapshots",
        "orderbook_snapshots_live",
        "active_weather_station_map",
        "weather_observation_snapshots_live",
        "weather_forecast_snapshots_live",
        "nws_daily_climate_reports",
    ]:
        frame = storage.fetch_table(table_name, limit=100000)
        counts.append({"table": table_name, "rows": len(frame)})
    safe_dataframe(pd.DataFrame(counts))
    live_books = storage.fetch_table("orderbook_snapshots_live", limit=100000)
    if live_books.empty:
        st.warning("No recorded live orderbook snapshots yet. Start: python main.py record-orderbooks --weather-only --interval-seconds 30")
    else:
        st.caption("Live full-orderbook recording coverage")
        coverage = live_books.groupby("market_ticker").agg(
            snapshots=("id", "count"),
            last_ts=("ts", "max"),
            avg_spread=("spread_cents", "mean"),
            avg_yes_bid_depth=("depth_yes_bid_1", "mean"),
            avg_yes_ask_depth=("depth_yes_ask_1", "mean"),
        ).reset_index()
        safe_dataframe(coverage.sort_values("snapshots", ascending=False).head(1000))
        total = len(live_books)
        markets = coverage["market_ticker"].nunique()
        if total >= 1000 and markets >= 20:
            st.success("Enough recorded orderbook data for preliminary passive replay across many markets. Still paper-only.")
        elif coverage["snapshots"].max() >= 100:
            st.info("Enough snapshots for preliminary passive replay on at least one market.")
        else:
            st.warning("Not enough recorded orderbook data yet.")
    candles = storage.fetch_table("historical_candlesticks", limit=5000)
    if not candles.empty:
        st.caption("Recent candlestick coverage")
        safe_dataframe(candles[["market_ticker", "ts", "period", "yes_bid", "yes_ask", "volume"]].head(1000))

with tabs[6]:
    st.subheader("Recorded Data / Edge Analysis")
    audits = storage.fetch_table("recorded_data_audits", limit=20)
    replay = storage.fetch_table("recorded_orderbook_replay_snapshots", limit=100000)
    sweeps = storage.fetch_table("recorded_strategy_sweeps", limit=5000)
    live_books = storage.fetch_table("orderbook_snapshots_live", limit=100000)
    cols = st.columns(4)
    cols[0].metric("Live book rows", len(live_books))
    cols[1].metric("Recorded replay rows", len(replay))
    cols[2].metric("Replay markets", replay["market_ticker"].nunique() if not replay.empty else 0)
    cols[3].metric("Sweep variants stored", len(sweeps))
    health = ProjectMaintenance(storage).collector_health(last_hours=24).payload
    st.caption(f"Collector health: {health.get('recommendation')}")
    if audits.empty:
        st.warning("No recorded-data audit yet. Run `python main.py audit-recorded-data`.")
    else:
        latest = audits.iloc[0]
        st.info(latest.get("verdict") or "Latest audit has no verdict.")
        safe_dataframe(flatten_json_columns(audits.head(5)))
    if replay.empty:
        st.warning("No recorded full-orderbook replay rows yet. Run `python main.py build-recorded-replay --last-days 3` after settlements exist.")
    else:
        label_counts = replay.get("settlement_source_type", pd.Series(dtype=object)).fillna("missing").value_counts().reset_index()
        label_counts.columns = ["settlement_source_type", "rows"]
        st.caption("Settlement source mix in recorded replay")
        safe_dataframe(label_counts)
        low_quality = replay[replay["data_quality_score"].fillna(0) < 0.85]
        if not low_quality.empty:
            st.warning("Some replay rows are below primary quality. Treat results as exploratory unless filtered.")
    if sweeps.empty:
        st.warning("No strategy sweep yet. Run `python main.py sweep-recorded --last-days 3`.")
    else:
        show_stale_sweeps = st.checkbox("Show stale/invalid recorded sweeps", value=False, key="show_stale_sweeps")
        if "is_stale" in sweeps.columns and not show_stale_sweeps:
            sweeps = sweeps[sweeps["is_stale"].fillna(0).astype(int).eq(0)]
        expanded = flatten_json_columns(sweeps)
        if expanded.empty:
            st.warning("Only stale/invalid sweep rows exist. Run `python main.py rebuild-clean-edge-analysis --last-days 3`.")
            top = expanded
        else:
            top = expanded.sort_values(["net_pnl", "fills"], ascending=[False, False]).head(25)
        st.caption("Top stored sweep rows")
        safe_dataframe(top)
        bad = expanded[(expanded.get("fills", pd.Series(dtype=float)).fillna(0) < 30) | (expanded.get("net_pnl", pd.Series(dtype=float)).fillna(0) <= 0)]
        if not bad.empty:
            st.warning("Not enough sample size or no positive net result for many variants. Do not trade live yet.")
    reports_dir = PROJECT_ROOT / "reports"
    reports = sorted(reports_dir.glob("edge_report_*.md"), reverse=True) if reports_dir.exists() else []
    if reports:
        st.caption("Latest edge reports")
        safe_dataframe(pd.DataFrame({"path": [str(path) for path in reports[:10]]}))
    st.markdown(
        """
        **Command suggestions**

        `python main.py audit-recorded-data`

        `python main.py build-recorded-replay --last-days 3`

        `python main.py sweep-recorded --last-days 3`

        `python main.py edge-report --last-days 3`
        """
    )

with tabs[7]:
    st.subheader("Trading Research Command Center")
    st.warning("Live trading disabled. Research/paper mode only.")
    readiness = TradingReadiness(storage).evaluate(last_days=7)
    cols = st.columns(3)
    cols[0].metric("Readiness", readiness.status)
    cols[1].metric("Replay rows", readiness.metrics.get("replay_rows", 0))
    cols[2].metric("Stale runs", readiness.metrics.get("stale_runs", 0))
    st.caption(readiness.message)
    if readiness.reasons:
        st.write("Why not ready / what is missing")
        safe_dataframe(pd.DataFrame({"reason": readiness.reasons}))
    st.code(readiness.next_command)

    st.caption("Data health")
    health_rows = [
        {"component": "orderbook_recorder", **ProjectMaintenance(storage).collector_health(last_hours=24).payload},
        {"component": "weather_recorders", **ProjectMaintenance(storage).weather_recorder_health(last_hours=24).payload},
    ]
    safe_dataframe(flatten_json_columns(pd.DataFrame(health_rows)))

    if st.button("Rank current opportunities", key="rank_current_opps"):
        with st.spinner("Ranking current active markets. Research/paper only."):
            ranked = OpportunityRanker(storage).rank(weather_only=True, max_markets=50)
        safe_dataframe(pd.DataFrame(ranked.rows))
        for warning in ranked.warnings[:10]:
            st.warning(warning)

    st.caption("Liquidity analysis")
    liquidity = LiquidityAnalyzer(storage).analyze(last_days=7, persist_exports=False)
    st.write(liquidity.summary)
    safe_dataframe(pd.DataFrame(liquidity.markets[:50]))

    st.caption("Signal future-price validation")
    validation = SignalValidator(storage).validate(last_days=7)
    safe_dataframe(pd.DataFrame(validation.summary_by_strategy))
