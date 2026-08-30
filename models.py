"""
models.py — Shared dataclasses/Pydantic models used across the system.
Keep flat. No inheritance trees.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional


class Venue(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class OrderStatus(str, Enum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    PARTIALLY_FILLED = "partially_filled"


@dataclass
class MarketSnapshot:
    venue: Venue
    market_id: str           # venue-native ID / ticker
    city: str                # e.g. "NYC"
    bucket_label: str        # e.g. ">=72°F" or "68-70°F"
    bucket_min: Optional[float]
    bucket_max: Optional[float]
    settlement_date: str     # YYYY-MM-DD
    best_bid: Optional[Decimal]   # 0-1 probability
    best_ask: Optional[Decimal]
    last_price: Optional[Decimal]
    yes_bid: Optional[Decimal]
    yes_ask: Optional[Decimal]
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_open: bool = True


@dataclass
class ModelOutput:
    city: str
    settlement_date: str
    expected_high: float          # °F
    std_dev: float                # °F
    bucket_probs: dict[str, float]  # bucket_label → probability (sums to ~1)
    confidence: float             # 0-1, lower = less reliable
    obs_temp: Optional[float]     # latest observed temp
    forecast_temp: Optional[float]
    observed_high_so_far: Optional[float] = None
    remaining_forecast_high: Optional[float] = None
    local_hour: Optional[int] = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TradeSignal:
    venue: Venue
    market_id: str
    city: str
    bucket_label: str
    side: Side
    fair_prob: float
    market_prob: float
    edge: float                 # fair - market (positive = we have edge)
    suggested_price: Decimal    # limit price to post
    suggested_contracts: int
    reason: str


@dataclass
class Order:
    venue: Venue
    venue_order_id: str
    client_order_id: str        # our UUID, idempotency key
    market_id: str
    city: str
    bucket_label: str
    side: Side
    price: Decimal              # 0-1
    contracts: int
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    filled_contracts: int = 0
    fill_price: Optional[Decimal] = None


@dataclass
class Fill:
    venue: Venue
    fill_id: str
    order_id: str
    market_id: str
    side: Side
    contracts: int
    price: Decimal
    fee: Decimal
    ts: datetime


@dataclass
class Position:
    venue: Venue
    market_id: str
    city: str
    bucket_label: str
    side: Side
    contracts: int
    avg_price: Decimal
    current_value: Optional[Decimal] = None  # from market price
    unrealized_pnl: Optional[Decimal] = None


@dataclass
class Balance:
    venue: Venue
    available: Decimal
    total: Decimal
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class WeatherObs:
    station_id: str
    city: str
    temp_f: float
    obs_time: datetime
    source: str   # "nws_obs", "iem"


@dataclass
class WeatherForecast:
    station_id: str
    city: str
    forecast_for: str   # YYYY-MM-DD
    high_f: float
    low_f: float
    hourly: list[dict]  # [{hour: datetime, temp_f: float}]
    source: str
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
