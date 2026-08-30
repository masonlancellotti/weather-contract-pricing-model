"""
config.py — All configuration validated at startup. Fail loudly on bad config.
"""
from __future__ import annotations
import os
from decimal import Decimal
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Safety ──────────────────────────────────────────────────────────────────
    live_enabled: bool = False
    kill_switch: bool = False

    # ── Kalshi ──────────────────────────────────────────────────────────────────
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_demo: bool = False

    # ── Polymarket ───────────────────────────────────────────────────────────────
    poly_private_key: str = ""
    poly_api_key: str = ""
    poly_api_secret: str = ""
    poly_api_passphrase: str = ""
    poly_chain_id: int = 137

    # ── Risk caps ────────────────────────────────────────────────────────────────
    max_contracts_per_order: int = Field(default=5, ge=1, le=50)
    max_order_size_usd: Decimal = Field(default=Decimal("10.0"))
    max_balance_fraction_per_order: float = Field(default=0.25, ge=0.05, le=1.0)
    max_venue_exposure_usd: Decimal = Field(default=Decimal("50.0"))
    max_city_exposure_usd: Optional[Decimal] = None
    min_order_notional_usd: Decimal = Field(default=Decimal("1.0"))
    min_edge_threshold: float = Field(default=0.06, ge=0.01, le=0.50)
    min_model_confidence: float = Field(default=0.80, ge=0.10, le=0.99)

    # ── Strategy ─────────────────────────────────────────────────────────────────
    active_cities: list[str] = Field(default=["NYC", "CHI"])
    weather_refresh_seconds: int = Field(default=300, ge=60)
    market_poll_seconds: int = Field(default=30, ge=10)
    max_market_snapshot_age_seconds: int = Field(default=900, ge=30)
    order_stale_seconds: int = Field(default=180, ge=30)
    entry_start_local_hour: int = Field(default=6, ge=0, le=23)
    entry_cutoff_local_hour: int = Field(default=15, ge=0, le=23)
    allow_close_only_after_cutoff: bool = False
    require_obs_by_local_hour: int = Field(default=8, ge=0, le=23)
    require_observed_high_by_local_hour: int = Field(default=10, ge=0, le=23)
    late_day_guard_local_hour: int = Field(default=18, ge=0, le=23)
    late_day_market_certainty_prob: float = Field(default=0.95, ge=0.50, le=0.999)
    bucket_guardrail_slack_f: float = Field(default=0.25, ge=0.0, le=2.0)

    # ── Weather data ─────────────────────────────────────────────────────────────
    noaa_cdo_token: str = ""

    # ── Misc ─────────────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    db_path: str = "./weather_trader.db"
    poly_live_trading_enabled: bool = False

    @field_validator("active_cities", mode="before")
    @classmethod
    def parse_cities(cls, v):
        if isinstance(v, str):
            return [c.strip().upper() for c in v.split(",") if c.strip()]
        return [c.upper() for c in v]

    @model_validator(mode="after")
    def warn_on_live(self) -> "Config":
        if self.max_city_exposure_usd is None:
            self.max_city_exposure_usd = self.max_venue_exposure_usd / 2
        if self.live_enabled:
            import logging
            logging.getLogger("config").warning(
                "⚠️  LIVE_ENABLED=true — real money orders will be placed"
            )
        return self

    @property
    def kalshi_base_url(self) -> str:
        if self.kalshi_demo:
            return "https://demo-api.kalshi.co/trade-api/v2"
        return "https://api.elections.kalshi.com/trade-api/v2"

    @property
    def kalshi_ws_url(self) -> str:
        if self.kalshi_demo:
            return "wss://demo-api.kalshi.co/trade-api/ws/v2"
        return "wss://api.elections.kalshi.com/trade-api/ws/v2"

    def load_kalshi_private_key(self) -> str:
        """Return PEM string for the Kalshi RSA private key."""
        path = Path(self.kalshi_private_key_path)
        if not path.exists():
            raise FileNotFoundError(f"Kalshi private key not found: {path}")
        return path.read_text()

    def has_kalshi_creds(self) -> bool:
        return bool(self.kalshi_api_key_id and self.kalshi_private_key_path)

    def has_poly_creds(self) -> bool:
        return bool(self.poly_private_key)


# Singleton — import this everywhere
cfg = Config()
