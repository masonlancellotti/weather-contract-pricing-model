from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import JSON, Column, DateTime, Float, Integer, MetaData, String, Table, Text, create_engine, insert, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from config import Settings, settings


metadata = MetaData()


def _table(name: str, *columns: Column) -> Table:
    return Table(
        name,
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("created_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False),
        *columns,
    )


markets = _table("markets", Column("ticker", String, unique=True), Column("event_ticker", String), Column("payload", JSON))
market_snapshots = _table("market_snapshots", Column("ticker", String), Column("snapshot_time", DateTime(timezone=True)), Column("payload", JSON))
orderbook_snapshots = _table("orderbook_snapshots", Column("ticker", String), Column("snapshot_time", DateTime(timezone=True)), Column("payload", JSON))
trades = _table("trades", Column("ticker", String), Column("trade_time", DateTime(timezone=True)), Column("payload", JSON))
weather_observations = _table("weather_observations", Column("station_code", String), Column("observed_at", DateTime(timezone=True)), Column("payload", JSON))
weather_forecasts = _table("weather_forecasts", Column("station_code", String), Column("forecast_time", DateTime(timezone=True)), Column("payload", JSON))
active_weather_station_map = _table(
    "active_weather_station_map",
    Column("market_ticker", String),
    Column("event_ticker", String),
    Column("city", String),
    Column("station_code", String),
    Column("station_name", String),
    Column("wfo", String),
    Column("timezone", String),
    Column("settlement_source_type", String),
    Column("source_url_or_hint", Text),
    Column("mapping_confidence", Float),
    Column("mapping_reason", Text),
    Column("warnings", Text),
    Column("parser_version", String),
    Column("updated_at", DateTime(timezone=True)),
)
weather_observation_snapshots_live = _table(
    "weather_observation_snapshots_live",
    Column("station_code", String, nullable=False),
    Column("station_name", String),
    Column("ts_observed", DateTime(timezone=True)),
    Column("ts_recorded", DateTime(timezone=True), nullable=False),
    Column("source", String),
    Column("source_url", Text),
    Column("temp_f", Float),
    Column("dewpoint_f", Float),
    Column("humidity", Float),
    Column("wind_speed_mph", Float),
    Column("wind_direction_degrees", Float),
    Column("wind_gust_mph", Float),
    Column("pressure_mb", Float),
    Column("visibility_miles", Float),
    Column("precip_1h", Float),
    Column("precip_3h", Float),
    Column("raw_text", Text),
    Column("raw_json", Text),
    Column("quality_score", Float),
    Column("warnings", Text),
)
weather_forecast_snapshots_live = _table(
    "weather_forecast_snapshots_live",
    Column("station_code", String, nullable=False),
    Column("station_name", String),
    Column("ts_forecast_created", DateTime(timezone=True)),
    Column("ts_recorded", DateTime(timezone=True), nullable=False),
    Column("forecast_valid_start", DateTime(timezone=True)),
    Column("forecast_valid_end", DateTime(timezone=True)),
    Column("source", String),
    Column("source_url", Text),
    Column("forecast_hour", Integer),
    Column("temp_f", Float),
    Column("dewpoint_f", Float),
    Column("humidity", Float),
    Column("wind_speed_mph", Float),
    Column("wind_direction_degrees", Float),
    Column("precip_probability", Float),
    Column("quantitative_precip", Float),
    Column("sky_cover", Float),
    Column("raw_json", Text),
    Column("raw_text", Text),
    Column("quality_score", Float),
    Column("warnings", Text),
)
parsed_contracts = _table("parsed_contracts", Column("market_ticker", String), Column("event_ticker", String), Column("parse_confidence", Float), Column("payload", JSON))
model_predictions = _table("model_predictions", Column("market_ticker", String), Column("prediction_time", DateTime(timezone=True)), Column("model_version", String), Column("payload", JSON))
signals = _table(
    "signals",
    Column("market_ticker", String),
    Column("signal_time", DateTime(timezone=True)),
    Column("strategy", String),
    Column("action", String),
    Column("edge_cents", Float),
    Column("edge_type", String),
    Column("execution_type", String),
    Column("confidence_level", String),
    Column("data_quality_score", Float),
    Column("settlement_quality_score", Float),
    Column("parser_version", String),
    Column("settlement_version", String),
    Column("strategy_version", String),
    Column("payload", JSON),
)
paper_orders = _table(
    "paper_orders",
    Column("market_ticker", String),
    Column("order_time", DateTime(timezone=True)),
    Column("status", String),
    Column("strategy", String),
    Column("edge_type", String),
    Column("execution_type", String),
    Column("action", String),
    Column("side", String),
    Column("intended_price", Float),
    Column("assumed_fill_price", Float),
    Column("contracts", Float),
    Column("fair_yes_price", Float),
    Column("edge_cents", Float),
    Column("fill_status", String),
    Column("reason", Text),
    Column("raw_json", Text),
    Column("payload", JSON),
)
paper_positions = _table(
    "paper_positions",
    Column("market_ticker", String),
    Column("side", String),
    Column("quantity", Float),
    Column("contracts", Float),
    Column("avg_price_cents", Float),
    Column("current_mark", Float),
    Column("unrealized_pnl", Float),
    Column("realized_pnl", Float),
    Column("settlement_status", String),
    Column("payload", JSON),
)
settlements = _table("settlements", Column("market_ticker", String), Column("settled_at", DateTime(timezone=True)), Column("result", String), Column("payload", JSON))
backtest_runs = _table(
    "backtest_runs",
    Column("run_name", String),
    Column("strategy", String),
    Column("start_date", String),
    Column("end_date", String),
    Column("mode", String),
    Column("data_quality_score", Float),
    Column("limitations", Text),
    Column("replay_data_type", String),
    Column("execution_assumption", String),
    Column("edge_type", String),
    Column("execution_type", String),
    Column("confidence_level", String),
    Column("settlement_label_quality", String),
    Column("parser_version", String),
    Column("settlement_version", String),
    Column("strategy_version", String),
    Column("parameter_hash", String),
    Column("is_stale", Integer, default=0),
    Column("stale_reason", Text),
    Column("payload", JSON),
)
historical_candlesticks = _table(
    "historical_candlesticks",
    Column("market_ticker", String, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("period", String),
    Column("open_yes_price", Float),
    Column("high_yes_price", Float),
    Column("low_yes_price", Float),
    Column("close_yes_price", Float),
    Column("yes_bid", Float),
    Column("yes_ask", Float),
    Column("no_bid", Float),
    Column("no_ask", Float),
    Column("volume", Float),
    Column("open_interest", Float),
    Column("raw_json", Text),
)
historical_trades = _table(
    "historical_trades",
    Column("market_ticker", String, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("price", Float),
    Column("count", Float),
    Column("yes_price", Float),
    Column("no_price", Float),
    Column("side", String),
    Column("raw_json", Text),
)
settlement_labels = _table(
    "settlement_labels",
    Column("market_ticker", String, nullable=False, unique=True),
    Column("event_ticker", String),
    Column("city", String),
    Column("station_code", String),
    Column("local_date", String),
    Column("variable_type", String),
    Column("contract_type", String),
    Column("threshold", Float),
    Column("comparator", String),
    Column("range_low", Float),
    Column("range_high", Float),
    Column("unit", String),
    Column("settlement_value", Float),
    Column("yes_result", Integer),
    Column("source", String),
    Column("primary_source_type", String),
    Column("confidence", Float),
    Column("warnings", Text),
    Column("raw_json", Text),
    Column("exact_source_available", Integer, default=0),
    Column("exact_source_type", String),
    Column("exact_source_report_id", String),
    Column("exact_settlement_value", Float),
    Column("fallback_source_type", String),
    Column("fallback_settlement_value", Float),
    Column("exact_vs_fallback_diff", Float),
    Column("settlement_version", String),
)
replay_snapshots = _table(
    "replay_snapshots",
    Column("market_ticker", String, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("yes_bid", Float),
    Column("yes_ask", Float),
    Column("yes_mid", Float),
    Column("no_bid", Float),
    Column("no_ask", Float),
    Column("last_trade_price", Float),
    Column("volume", Float),
    Column("open_interest", Float),
    Column("weather_features_json", Text),
    Column("market_features_json", Text),
    Column("replay_data_type", String),
    Column("full_orderbook_json", Text),
)
orderbook_snapshots_live = _table(
    "orderbook_snapshots_live",
    Column("market_ticker", String, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("yes_bids_json", Text),
    Column("no_bids_json", Text),
    Column("yes_best_bid", Float),
    Column("yes_best_ask", Float),
    Column("no_best_bid", Float),
    Column("no_best_ask", Float),
    Column("spread_cents", Float),
    Column("mid_cents", Float),
    Column("depth_yes_bid_1", Float),
    Column("depth_yes_ask_1", Float),
    Column("depth_no_bid_1", Float),
    Column("depth_no_ask_1", Float),
    Column("total_yes_bid_depth", Float),
    Column("total_no_bid_depth", Float),
    Column("raw_json", Text),
    Column("source", String, default="kalshi_current_orderbook"),
)
nws_daily_climate_reports = _table(
    "nws_daily_climate_reports",
    Column("station_code", String, nullable=False),
    Column("local_date", String, nullable=False),
    Column("report_product_id", String),
    Column("office", String),
    Column("report_url", Text),
    Column("issued_at", DateTime(timezone=True)),
    Column("raw_text", Text),
    Column("parsed_high_temp", Float),
    Column("parsed_low_temp", Float),
    Column("parsed_precip", Float),
    Column("parsed_snowfall", Float),
    Column("parser_confidence", Float),
    Column("warnings", Text),
)
recorded_data_audits = _table(
    "recorded_data_audits",
    Column("ts", DateTime(timezone=True)),
    Column("total_snapshots", Integer),
    Column("unique_markets", Integer),
    Column("first_snapshot_ts", DateTime(timezone=True)),
    Column("last_snapshot_ts", DateTime(timezone=True)),
    Column("markets_with_settlements", Integer),
    Column("markets_without_settlements", Integer),
    Column("markets_with_100_plus_snapshots", Integer),
    Column("markets_with_500_plus_snapshots", Integer),
    Column("verdict", Text),
    Column("raw_json", Text),
)
recorded_orderbook_replay_snapshots = _table(
    "recorded_orderbook_replay_snapshots",
    Column("market_ticker", String, nullable=False),
    Column("event_ticker", String),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("city", String),
    Column("station_code", String),
    Column("local_date", String),
    Column("variable_type", String),
    Column("contract_type", String),
    Column("threshold", Float),
    Column("comparator", String),
    Column("range_low", Float),
    Column("range_high", Float),
    Column("unit", String),
    Column("yes_best_bid", Float),
    Column("yes_best_ask", Float),
    Column("no_best_bid", Float),
    Column("no_best_ask", Float),
    Column("yes_mid", Float),
    Column("spread_cents", Float),
    Column("yes_bids_json", Text),
    Column("no_bids_json", Text),
    Column("total_yes_bid_depth", Float),
    Column("total_no_bid_depth", Float),
    Column("depth_yes_bid_1", Float),
    Column("depth_yes_ask_1", Float),
    Column("current_temp_asof", Float),
    Column("max_temp_so_far_asof", Float),
    Column("min_temp_so_far_asof", Float),
    Column("temp_1h_ago_asof", Float),
    Column("temp_3h_ago_asof", Float),
    Column("temp_trend_1h", Float),
    Column("temp_trend_3h", Float),
    Column("local_hour", Float),
    Column("minutes_to_close", Float),
    Column("minutes_to_settlement", Float),
    Column("threshold_gap_current", Float),
    Column("threshold_gap_max_so_far", Float),
    Column("threshold_gap_min_so_far", Float),
    Column("is_threshold_already_hit_asof", Integer),
    Column("weather_feature_source", String),
    Column("latest_observation_recorded_at", DateTime(timezone=True)),
    Column("latest_forecast_recorded_at", DateTime(timezone=True)),
    Column("forecast_high_remaining_f", Float),
    Column("forecast_low_remaining_f", Float),
    Column("forecast_max_next_6h_f", Float),
    Column("forecast_min_next_6h_f", Float),
    Column("forecast_source", String),
    Column("weather_asof_quality_score", Float),
    Column("settlement_value", Float),
    Column("yes_result", Integer),
    Column("settlement_confidence", Float),
    Column("settlement_source_type", String),
    Column("parser_version", String),
    Column("settlement_version", String),
    Column("data_quality_score", Float),
    Column("warnings", Text),
    Column("raw_json", Text),
)
recorded_strategy_sweeps = _table(
    "recorded_strategy_sweeps",
    Column("ts", DateTime(timezone=True)),
    Column("strategy", String),
    Column("mode", String),
    Column("params_json", Text),
    Column("start_date", String),
    Column("end_date", String),
    Column("label_quality", String),
    Column("markets", Integer),
    Column("snapshots", Integer),
    Column("signals", Integer),
    Column("fills", Integer),
    Column("gross_pnl", Float),
    Column("fees", Float),
    Column("net_pnl", Float),
    Column("roi", Float),
    Column("win_rate", Float),
    Column("max_drawdown", Float),
    Column("robustness_verdict", Text),
    Column("recommendation", String),
    Column("parser_version", String),
    Column("settlement_version", String),
    Column("strategy_version", String),
    Column("parameter_hash", String),
    Column("is_stale", Integer, default=0),
    Column("stale_reason", Text),
    Column("raw_json", Text),
)
collector_state = _table(
    "collector_state",
    Column("collector_name", String, unique=True),
    Column("started_at", DateTime(timezone=True)),
    Column("last_heartbeat_at", DateTime(timezone=True)),
    Column("last_snapshot_at", DateTime(timezone=True)),
    Column("cycles_completed", Integer),
    Column("snapshots_this_run", Integer),
    Column("markets_tracked", Integer),
    Column("current_task", String),
    Column("status", String),
    Column("error_message", Text),
    Column("updated_at", DateTime(timezone=True)),
)
backtest_trades = _table(
    "backtest_trades",
    Column("run_id", Integer),
    Column("market_ticker", String),
    Column("strategy", String),
    Column("pnl_cents", Float),
    Column("ts", DateTime(timezone=True)),
    Column("action", String),
    Column("side", String),
    Column("contracts", Float),
    Column("entry_price", Float),
    Column("exit_price", Float),
    Column("settlement_value", Float),
    Column("yes_result", Integer),
    Column("gross_pnl", Float),
    Column("fees", Float),
    Column("net_pnl", Float),
    Column("edge_cents", Float),
    Column("fair_yes_price", Float),
    Column("edge_type", String),
    Column("execution_type", String),
    Column("confidence_level", String),
    Column("data_quality_score", Float),
    Column("settlement_quality_score", Float),
    Column("parser_version", String),
    Column("settlement_version", String),
    Column("strategy_version", String),
    Column("future_mid_5m", Float),
    Column("future_mid_15m", Float),
    Column("future_mid_30m", Float),
    Column("future_mid_60m", Float),
    Column("final_mid_before_close", Float),
    Column("beat_5m", Integer),
    Column("beat_15m", Integer),
    Column("beat_30m", Integer),
    Column("beat_60m", Integer),
    Column("beat_close", Integer),
    Column("future_price_edge_cents", Float),
    Column("reason", Text),
    Column("raw_json", Text),
    Column("payload", JSON),
)


class Storage:
    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg
        self.engine: Engine = create_engine(cfg.database_url, future=True)

    def init_db(self) -> None:
        metadata.create_all(self.engine)
        self._run_migrations()

    def insert_json(self, table_name: str, payload: dict[str, Any], **columns: Any) -> int:
        self.init_db()
        table = metadata.tables[table_name]
        values = {**columns, "payload": _jsonable(payload)}
        with self.engine.begin() as conn:
            result = conn.execute(insert(table).values(**values))
            return int(result.inserted_primary_key[0])

    def save_market(self, market: dict[str, Any]) -> int:
        self.init_db()
        payload = _jsonable(market)
        ticker = str(market.get("ticker") or "")
        values = {"ticker": ticker, "event_ticker": str(market.get("event_ticker") or ""), "payload": payload}
        stmt = sqlite_insert(markets).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["ticker"],
            set_={"event_ticker": stmt.excluded.event_ticker, "payload": stmt.excluded.payload},
        )
        with self.engine.begin() as conn:
            result = conn.execute(stmt)
            if result.inserted_primary_key and result.inserted_primary_key[0]:
                return int(result.inserted_primary_key[0])
        return 0

    def save_parsed_contract(self, contract: Any) -> int:
        payload = contract.model_dump(mode="json") if hasattr(contract, "model_dump") else contract.dict()
        return self.insert_json(
            "parsed_contracts",
            payload,
            market_ticker=payload.get("market_ticker"),
            event_ticker=payload.get("event_ticker"),
            parse_confidence=payload.get("parse_confidence", 0.0),
        )

    def save_signal(self, signal: Any) -> int:
        payload = signal.to_dict() if hasattr(signal, "to_dict") else dict(signal)
        return self.insert_json(
            "signals",
            payload,
            market_ticker=payload.get("market_ticker"),
            signal_time=_parse_dt(payload.get("timestamp")) or datetime.now(timezone.utc),
            strategy=payload.get("strategy"),
            action=payload.get("action"),
            edge_cents=payload.get("edge_cents"),
            edge_type=payload.get("edge_type"),
            execution_type=payload.get("execution_type"),
            confidence_level=payload.get("confidence_level"),
            data_quality_score=payload.get("data_quality_score"),
            settlement_quality_score=payload.get("settlement_quality_score"),
            parser_version=payload.get("parser_version"),
            settlement_version=payload.get("settlement_version"),
            strategy_version=payload.get("strategy_version"),
        )

    def fetch_table(self, table_name: str, limit: int = 5000) -> pd.DataFrame:
        self.init_db()
        table = metadata.tables[table_name]
        with self.engine.connect() as conn:
            rows = conn.execute(select(table).order_by(table.c.id.desc()).limit(limit)).mappings().all()
        return pd.DataFrame(rows)

    def fetch_sql(self, sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
        self.init_db()
        with self.engine.connect() as conn:
            return pd.read_sql_query(text(sql), conn, params=params or {})

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.init_db()
        with self.engine.begin() as conn:
            conn.execute(text(sql), params or {})

    def upsert_historical_candlestick(self, row: dict[str, Any]) -> None:
        self.init_db()
        stmt = sqlite_insert(historical_candlesticks).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["market_ticker", "ts", "period"],
            set_={key: getattr(stmt.excluded, key) for key in row if key not in {"id", "created_at"}},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def upsert_settlement_label(self, row: dict[str, Any]) -> None:
        self.init_db()
        stmt = sqlite_insert(settlement_labels).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["market_ticker"],
            set_={key: getattr(stmt.excluded, key) for key in row if key not in {"id", "created_at"}},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def upsert_replay_snapshot(self, row: dict[str, Any]) -> None:
        self.init_db()
        stmt = sqlite_insert(replay_snapshots).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["market_ticker", "ts"],
            set_={key: getattr(stmt.excluded, key) for key in row if key not in {"id", "created_at"}},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def upsert_live_orderbook_snapshot(self, row: dict[str, Any]) -> None:
        self.init_db()
        stmt = sqlite_insert(orderbook_snapshots_live).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["market_ticker", "ts"],
            set_={key: getattr(stmt.excluded, key) for key in row if key not in {"id", "created_at"}},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def upsert_nws_daily_climate_report(self, row: dict[str, Any]) -> None:
        self.init_db()
        stmt = sqlite_insert(nws_daily_climate_reports).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["station_code", "local_date"],
            set_={key: getattr(stmt.excluded, key) for key in row if key not in {"id", "created_at"}},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def insert_recorded_data_audit(self, row: dict[str, Any]) -> int:
        self.init_db()
        with self.engine.begin() as conn:
            result = conn.execute(insert(recorded_data_audits).values(**row))
            return int(result.inserted_primary_key[0])

    def upsert_recorded_orderbook_replay_snapshot(self, row: dict[str, Any]) -> None:
        self.init_db()
        stmt = sqlite_insert(recorded_orderbook_replay_snapshots).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["market_ticker", "ts"],
            set_={key: getattr(stmt.excluded, key) for key in row if key not in {"id", "created_at"}},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def upsert_recorded_orderbook_replay_snapshots(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        self.init_db()
        if len(rows) > 100:
            for start in range(0, len(rows), 100):
                self.upsert_recorded_orderbook_replay_snapshots(rows[start : start + 100])
            return
        keys = set().union(*(row.keys() for row in rows))
        normalized = [{key: row.get(key) for key in keys} for row in rows]
        stmt = sqlite_insert(recorded_orderbook_replay_snapshots).values(normalized)
        stmt = stmt.on_conflict_do_update(
            index_elements=["market_ticker", "ts"],
            set_={key: getattr(stmt.excluded, key) for key in keys if key not in {"id", "created_at"}},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def insert_recorded_strategy_sweep(self, row: dict[str, Any]) -> int:
        self.init_db()
        with self.engine.begin() as conn:
            result = conn.execute(insert(recorded_strategy_sweeps).values(**row))
            return int(result.inserted_primary_key[0])

    def upsert_collector_state(self, row: dict[str, Any]) -> None:
        self.init_db()
        stmt = sqlite_insert(collector_state).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["collector_name"],
            set_={key: getattr(stmt.excluded, key) for key in row if key not in {"id", "created_at"}},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def upsert_active_weather_station_map(self, row: dict[str, Any]) -> None:
        self.init_db()
        stmt = sqlite_insert(active_weather_station_map).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["market_ticker"],
            set_={key: getattr(stmt.excluded, key) for key in row if key not in {"id", "created_at"}},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def insert_weather_observation_snapshot(self, row: dict[str, Any]) -> int:
        self.init_db()
        with self.engine.begin() as conn:
            result = conn.execute(insert(weather_observation_snapshots_live).values(**row))
            return int(result.inserted_primary_key[0])

    def insert_weather_forecast_snapshots(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        self.init_db()
        with self.engine.begin() as conn:
            conn.execute(insert(weather_forecast_snapshots_live).values(rows))
        return len(rows)

    def insert_backtest_trade(self, row: dict[str, Any]) -> int:
        self.init_db()
        with self.engine.begin() as conn:
            result = conn.execute(insert(backtest_trades).values(**row))
            return int(result.inserted_primary_key[0])

    def insert_historical_trade(self, row: dict[str, Any]) -> int:
        self.init_db()
        with self.engine.begin() as conn:
            result = conn.execute(insert(historical_trades).values(**row))
            return int(result.inserted_primary_key[0])

    def _run_migrations(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_historical_candlesticks_key ON historical_candlesticks(market_ticker, ts, period)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_replay_snapshots_key ON replay_snapshots(market_ticker, ts)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_orderbook_snapshots_live_key ON orderbook_snapshots_live(market_ticker, ts)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_orderbook_snapshots_live_ts ON orderbook_snapshots_live(ts)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_orderbook_snapshots_live_market ON orderbook_snapshots_live(market_ticker)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_nws_daily_climate_reports_key ON nws_daily_climate_reports(station_code, local_date)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_recorded_replay_key ON recorded_orderbook_replay_snapshots(market_ticker, ts)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recorded_replay_ts ON recorded_orderbook_replay_snapshots(ts)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recorded_replay_market ON recorded_orderbook_replay_snapshots(market_ticker)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_collector_state_name ON collector_state(collector_name)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_active_weather_station_map_market ON active_weather_station_map(market_ticker)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_active_weather_station_map_station ON active_weather_station_map(station_code)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_weather_obs_live_station_observed ON weather_observation_snapshots_live(station_code, ts_observed)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_weather_obs_live_station_recorded ON weather_observation_snapshots_live(station_code, ts_recorded)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_weather_obs_live_recorded ON weather_observation_snapshots_live(ts_recorded)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_weather_forecast_live_station_recorded ON weather_forecast_snapshots_live(station_code, ts_recorded)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_weather_forecast_live_station_valid ON weather_forecast_snapshots_live(station_code, forecast_valid_start)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_weather_forecast_live_recorded ON weather_forecast_snapshots_live(ts_recorded)"))
            for table_name, columns in _MIGRATION_COLUMNS.items():
                existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()}
                for column_name, column_sql in columns.items():
                    if column_name not in existing:
                        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"))


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload, default=str))


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


_MIGRATION_COLUMNS: dict[str, dict[str, str]] = {
    "backtest_runs": {
        "mode": "TEXT",
        "data_quality_score": "REAL",
        "limitations": "TEXT",
        "replay_data_type": "TEXT",
        "execution_assumption": "TEXT",
        "edge_type": "TEXT",
        "execution_type": "TEXT",
        "confidence_level": "TEXT",
        "settlement_label_quality": "TEXT",
        "parser_version": "TEXT",
        "settlement_version": "TEXT",
        "strategy_version": "TEXT",
        "parameter_hash": "TEXT",
        "is_stale": "INTEGER DEFAULT 0",
        "stale_reason": "TEXT",
    },
    "backtest_trades": {
        "ts": "DATETIME",
        "action": "TEXT",
        "side": "TEXT",
        "contracts": "REAL",
        "entry_price": "REAL",
        "exit_price": "REAL",
        "settlement_value": "REAL",
        "yes_result": "INTEGER",
        "gross_pnl": "REAL",
        "fees": "REAL",
        "net_pnl": "REAL",
        "edge_cents": "REAL",
        "fair_yes_price": "REAL",
        "edge_type": "TEXT",
        "execution_type": "TEXT",
        "confidence_level": "TEXT",
        "data_quality_score": "REAL",
        "settlement_quality_score": "REAL",
        "parser_version": "TEXT",
        "settlement_version": "TEXT",
        "strategy_version": "TEXT",
        "future_mid_5m": "REAL",
        "future_mid_15m": "REAL",
        "future_mid_30m": "REAL",
        "future_mid_60m": "REAL",
        "final_mid_before_close": "REAL",
        "beat_5m": "INTEGER",
        "beat_15m": "INTEGER",
        "beat_30m": "INTEGER",
        "beat_60m": "INTEGER",
        "beat_close": "INTEGER",
        "future_price_edge_cents": "REAL",
        "reason": "TEXT",
        "raw_json": "TEXT",
    },
    "signals": {
        "edge_type": "TEXT",
        "execution_type": "TEXT",
        "confidence_level": "TEXT",
        "data_quality_score": "REAL",
        "settlement_quality_score": "REAL",
        "parser_version": "TEXT",
        "settlement_version": "TEXT",
        "strategy_version": "TEXT",
    },
    "paper_orders": {
        "strategy": "TEXT",
        "edge_type": "TEXT",
        "execution_type": "TEXT",
        "action": "TEXT",
        "side": "TEXT",
        "intended_price": "REAL",
        "assumed_fill_price": "REAL",
        "contracts": "REAL",
        "fair_yes_price": "REAL",
        "edge_cents": "REAL",
        "fill_status": "TEXT",
        "reason": "TEXT",
        "raw_json": "TEXT",
    },
    "paper_positions": {
        "contracts": "REAL",
        "current_mark": "REAL",
        "unrealized_pnl": "REAL",
        "realized_pnl": "REAL",
        "settlement_status": "TEXT",
    },
    "settlement_labels": {
        "contract_type": "TEXT",
        "range_low": "REAL",
        "range_high": "REAL",
        "primary_source_type": "TEXT",
        "exact_source_available": "INTEGER DEFAULT 0",
        "exact_source_type": "TEXT",
        "exact_source_report_id": "TEXT",
        "exact_settlement_value": "REAL",
        "fallback_source_type": "TEXT",
        "fallback_settlement_value": "REAL",
        "exact_vs_fallback_diff": "REAL",
        "settlement_version": "TEXT",
    },
    "replay_snapshots": {
        "replay_data_type": "TEXT",
        "full_orderbook_json": "TEXT",
    },
    "recorded_orderbook_replay_snapshots": {
        "contract_type": "TEXT",
        "range_low": "REAL",
        "range_high": "REAL",
        "parser_version": "TEXT",
        "settlement_version": "TEXT",
        "weather_feature_source": "TEXT",
        "latest_observation_recorded_at": "DATETIME",
        "latest_forecast_recorded_at": "DATETIME",
        "forecast_high_remaining_f": "REAL",
        "forecast_low_remaining_f": "REAL",
        "forecast_max_next_6h_f": "REAL",
        "forecast_min_next_6h_f": "REAL",
        "forecast_source": "TEXT",
        "weather_asof_quality_score": "REAL",
    },
    "recorded_strategy_sweeps": {
        "parser_version": "TEXT",
        "settlement_version": "TEXT",
        "strategy_version": "TEXT",
        "parameter_hash": "TEXT",
        "is_stale": "INTEGER DEFAULT 0",
        "stale_reason": "TEXT",
    },
}
