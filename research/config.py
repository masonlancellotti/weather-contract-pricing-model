from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - installed via requirements for normal use
    load_dotenv = None


if load_dotenv:
    load_dotenv()


PROJECT_ROOT = Path(__file__).resolve().parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


@dataclass(frozen=True)
class Settings:
    kalshi_api_key_id: str | None
    kalshi_private_key_path: Path | None
    kalshi_env: str
    enable_live_trading: bool
    allow_market_orders: bool
    database_url: str
    cache_dir: Path

    max_trade_dollars: float
    max_market_exposure: float
    max_total_exposure: float
    max_daily_loss: float
    min_edge_cents: int
    min_spread_cents: int
    max_weather_data_age_minutes: int
    orderbook_record_interval_seconds: int
    orderbook_record_weather_only: bool
    orderbook_record_max_markets: int
    orderbook_record_full_depth: bool
    kalshi_markets_refresh_seconds: int
    kalshi_markets_refresh_failure_backoff_seconds: int
    kalshi_orderbook_sleep_between_requests_ms: int
    kalshi_max_retries: int
    kalshi_backoff_max_seconds: int
    nws_timeout_seconds: int
    nws_max_retries: int
    nws_backoff_max_seconds: int
    weather_cache_ttl_seconds: int
    collector_log_every_orderbook: bool
    collector_log_summary_every_cycles: int
    min_settlement_confidence_primary: float
    collector_default_duration_hours: float
    collector_scan_interval_minutes: int
    collector_maintenance_interval_minutes: int
    collector_settlement_lookback_days: int
    passive_assume_touch_fill: bool
    passive_default_fill_haircut: float
    passive_adverse_selection_penalty_cents: float
    passive_require_traded_through: bool
    passive_min_spread_cents: int
    passive_min_displayed_depth: float
    max_paper_dollars_per_market: float
    max_paper_total_exposure: float
    max_paper_daily_loss: float
    min_edge_after_buffers_cents: float
    min_settlement_confidence: float
    max_forecast_data_age_minutes: int

    kalshi_prod_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_demo_base_url: str = "https://demo-api.kalshi.co/trade-api/v2"

    @classmethod
    def from_env(cls) -> "Settings":
        private_key = os.getenv("KALSHI_PRIVATE_KEY_PATH") or None
        return cls(
            kalshi_api_key_id=os.getenv("KALSHI_API_KEY_ID") or None,
            kalshi_private_key_path=Path(private_key).expanduser() if private_key else None,
            kalshi_env=(os.getenv("KALSHI_ENV") or "prod").lower(),
            enable_live_trading=_env_bool("ENABLE_LIVE_TRADING", False),
            allow_market_orders=_env_bool("ALLOW_MARKET_ORDERS", False),
            database_url=os.getenv("DATABASE_URL", "sqlite:///kalshi_weather_edge.db"),
            cache_dir=PROJECT_ROOT / ".cache",
            max_trade_dollars=_env_float("MAX_TRADE_DOLLARS", 5.0),
            max_market_exposure=_env_float("MAX_MARKET_EXPOSURE", 20.0),
            max_total_exposure=_env_float("MAX_TOTAL_EXPOSURE", 100.0),
            max_daily_loss=_env_float("MAX_DAILY_LOSS", 25.0),
            min_edge_cents=_env_int("MIN_EDGE_CENTS", 7),
            min_spread_cents=_env_int("MIN_SPREAD_CENTS", 4),
            max_weather_data_age_minutes=_env_int("MAX_WEATHER_DATA_AGE_MINUTES", 15),
            orderbook_record_interval_seconds=_env_int("ORDERBOOK_RECORD_INTERVAL_SECONDS", 30),
            orderbook_record_weather_only=_env_bool("ORDERBOOK_RECORD_WEATHER_ONLY", True),
            orderbook_record_max_markets=_env_int("ORDERBOOK_RECORD_MAX_MARKETS", 100),
            orderbook_record_full_depth=_env_bool("ORDERBOOK_RECORD_FULL_DEPTH", True),
            kalshi_markets_refresh_seconds=_env_int("KALSHI_MARKETS_REFRESH_SECONDS", 300),
            kalshi_markets_refresh_failure_backoff_seconds=_env_int("KALSHI_MARKETS_REFRESH_FAILURE_BACKOFF_SECONDS", 600),
            kalshi_orderbook_sleep_between_requests_ms=_env_int("KALSHI_ORDERBOOK_SLEEP_BETWEEN_REQUESTS_MS", 100),
            kalshi_max_retries=_env_int("KALSHI_MAX_RETRIES", 5),
            kalshi_backoff_max_seconds=_env_int("KALSHI_BACKOFF_MAX_SECONDS", 60),
            nws_timeout_seconds=_env_int("NWS_TIMEOUT_SECONDS", 30),
            nws_max_retries=_env_int("NWS_MAX_RETRIES", 3),
            nws_backoff_max_seconds=_env_int("NWS_BACKOFF_MAX_SECONDS", 60),
            weather_cache_ttl_seconds=_env_int("WEATHER_CACHE_TTL_SECONDS", 300),
            collector_log_every_orderbook=_env_bool("COLLECTOR_LOG_EVERY_ORDERBOOK", False),
            collector_log_summary_every_cycles=_env_int("COLLECTOR_LOG_SUMMARY_EVERY_CYCLES", 10),
            min_settlement_confidence_primary=_env_float("MIN_SETTLEMENT_CONFIDENCE_PRIMARY", 0.85),
            collector_default_duration_hours=_env_float("COLLECTOR_DEFAULT_DURATION_HOURS", 72.0),
            collector_scan_interval_minutes=_env_int("COLLECTOR_SCAN_INTERVAL_MINUTES", 5),
            collector_maintenance_interval_minutes=_env_int("COLLECTOR_MAINTENANCE_INTERVAL_MINUTES", 60),
            collector_settlement_lookback_days=_env_int("COLLECTOR_SETTLEMENT_LOOKBACK_DAYS", 10),
            passive_assume_touch_fill=_env_bool("PASSIVE_ASSUME_TOUCH_FILL", False),
            passive_default_fill_haircut=_env_float("PASSIVE_DEFAULT_FILL_HAIRCUT", 0.25),
            passive_adverse_selection_penalty_cents=_env_float("PASSIVE_ADVERSE_SELECTION_PENALTY_CENTS", 2.0),
            passive_require_traded_through=_env_bool("PASSIVE_REQUIRE_TRADED_THROUGH", True),
            passive_min_spread_cents=_env_int("PASSIVE_MIN_SPREAD_CENTS", 8),
            passive_min_displayed_depth=_env_float("PASSIVE_MIN_DISPLAYED_DEPTH", 5.0),
            max_paper_dollars_per_market=_env_float("MAX_PAPER_DOLLARS_PER_MARKET", 10.0),
            max_paper_total_exposure=_env_float("MAX_PAPER_TOTAL_EXPOSURE", 100.0),
            max_paper_daily_loss=_env_float("MAX_PAPER_DAILY_LOSS", 25.0),
            min_edge_after_buffers_cents=_env_float("MIN_EDGE_AFTER_BUFFERS_CENTS", 7.0),
            min_settlement_confidence=_env_float("MIN_SETTLEMENT_CONFIDENCE", 0.85),
            max_forecast_data_age_minutes=_env_int("MAX_FORECAST_DATA_AGE_MINUTES", 60),
        )

    @property
    def kalshi_base_url(self) -> str:
        if self.kalshi_env == "demo":
            return self.kalshi_demo_base_url
        return self.kalshi_prod_base_url

    @property
    def sqlite_path(self) -> Path:
        parsed = urlparse(self.database_url)
        if parsed.scheme != "sqlite":
            raise ValueError("Only sqlite DATABASE_URL is supported in this MVP.")
        if parsed.path in {"", "/"}:
            raise ValueError("sqlite DATABASE_URL must include a file path.")
        if parsed.netloc:
            return Path(f"//{parsed.netloc}{parsed.path}")
        path = Path(parsed.path.lstrip("/"))
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    def require_live_trading_enabled(self) -> None:
        if not self.enable_live_trading:
            raise RuntimeError("Live trading is disabled. Set ENABLE_LIVE_TRADING=true only after paper trading is approved.")


settings = Settings.from_env()
