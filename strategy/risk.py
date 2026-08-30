"""
strategy/risk.py — Risk engine. Hard caps. Kill switch. Safe mode.

RULES:
  - Check kill switch before every order
  - Check per-order size caps
  - Check per-venue exposure caps
  - Check available balance
  - Prevent duplicate/idempotent order submissions
  - Stale order detection and cancellation
  - Safe mode: set on serious errors, prevents new orders
"""
from __future__ import annotations
from collections import defaultdict
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from config import cfg
from models import TradeSignal, Venue
from storage import DB

log = logging.getLogger("strategy.risk")

UTC = timezone.utc

# Module-level safe mode flag (in-process only — set on serious errors)
_safe_mode: bool = False
_safe_mode_reason: str = ""
_critical_failures: dict[str, list[datetime]] = defaultdict(list)


def is_safe_mode() -> bool:
    return _safe_mode


def set_safe_mode(reason: str):
    global _safe_mode, _safe_mode_reason
    _safe_mode = True
    _safe_mode_reason = reason
    log.error("SAFE MODE ACTIVATED: %s", reason)


def clear_safe_mode():
    global _safe_mode, _safe_mode_reason
    _safe_mode = False
    _safe_mode_reason = ""
    log.warning("Safe mode cleared manually")


def safe_mode_reason() -> str:
    return _safe_mode_reason


def record_critical_venue_failure(venue: Venue, reason: str):
    now = datetime.now(UTC)
    failures = [
        ts for ts in _critical_failures[venue.value]
        if (now - ts).total_seconds() <= 300
    ]
    failures.append(now)
    _critical_failures[venue.value] = failures
    log.error("Critical %s failure (%d/3): %s", venue.value, len(failures), reason)
    if len(failures) >= 3:
        set_safe_mode(f"repeated_{venue.value}_failures")


def clear_critical_venue_failures(venue: Venue):
    _critical_failures.pop(venue.value, None)


def check_kill_switch() -> bool:
    """Returns True if we should stop — kill switch is on or safe mode."""
    if cfg.kill_switch:
        log.warning("Kill switch is ON — no orders will be placed")
        return True
    if _safe_mode:
        log.warning("Safe mode active (%s) — no orders will be placed", _safe_mode_reason)
        return True
    return False


def check_live_enabled() -> bool:
    """Returns True if live trading is enabled."""
    if not cfg.live_enabled:
        log.debug("LIVE_ENABLED=false — order suppressed (paper mode)")
        return False
    return True


def pre_flight_check(
    signal: TradeSignal,
    available_balance: Decimal,
    db: DB,
) -> tuple[bool, str]:
    """
    Full pre-flight check before placing an order.
    Returns (ok: bool, reason: str).
    All checks must pass.
    """
    # 1. Kill switch / safe mode
    if check_kill_switch():
        return False, "kill_switch_or_safe_mode"

    # 2. Live enabled
    if not check_live_enabled():
        return False, "live_not_enabled"

    if signal.venue == Venue.POLYMARKET:
        from venues.polymarket import live_trading_enabled
        if not live_trading_enabled():
            return False, "polymarket_trading_disabled"

    # 3. Order size cap (USD value at risk)
    order_value = signal.suggested_price * signal.suggested_contracts
    if order_value > cfg.max_order_size_usd:
        return False, f"order_value {order_value:.2f} > max {cfg.max_order_size_usd}"

    # 4. Per-venue exposure cap
    current_exposure = db.get_total_exposure(signal.venue)
    if current_exposure + order_value > cfg.max_venue_exposure_usd:
        return False, (
            f"venue_exposure {current_exposure:.2f}+{order_value:.2f} "
            f"> max {cfg.max_venue_exposure_usd}"
        )

    current_city_exposure = db.get_city_exposure(signal.venue, signal.city)
    if current_city_exposure + order_value > cfg.max_city_exposure_usd:
        return False, (
            f"city_exposure {current_city_exposure:.2f}+{order_value:.2f} "
            f"> max {cfg.max_city_exposure_usd}"
        )

    # 5. Available balance
    if available_balance < order_value:
        return False, f"insufficient_balance {available_balance:.2f} < {order_value:.2f}"

    bankroll_cap = available_balance * Decimal(str(cfg.max_balance_fraction_per_order))
    if order_value > bankroll_cap:
        return False, (
            f"order_value {order_value:.2f} > bankroll_cap {bankroll_cap:.2f}"
        )

    # 6. Contracts per order cap
    if signal.suggested_contracts > cfg.max_contracts_per_order:
        return False, f"contracts {signal.suggested_contracts} > max {cfg.max_contracts_per_order}"

    # 7. Price sanity check — price must be 0.01–0.99
    price_f = float(signal.suggested_price)
    if not (0.01 <= price_f <= 0.99):
        return False, f"insane_price {price_f}"

    # 8. Allow only one open order or existing position per venue/market/side at a time.
    open_orders = db.get_open_orders(signal.venue)
    for o in open_orders:
        if o["venue"] != signal.venue.value:
            continue
        if o["market_id"] == signal.market_id and o["side"] == signal.side.value:
            return False, f"duplicate_market_side_order on {signal.market_id} {signal.side.value}"

    market_side_exposure = db.get_market_side_exposure(signal.venue, signal.market_id, signal.side)
    if market_side_exposure > 0:
        return False, f"existing_market_side_exposure {market_side_exposure:.2f}"

    return True, "ok"


def detect_stale_orders(db: DB) -> list[str]:
    """
    Return list of client_order_ids that are stale (older than ORDER_STALE_SECONDS).
    """
    now = datetime.now(UTC)
    stale = []
    for o in db.get_open_orders():
        try:
            created = datetime.fromisoformat(o["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_seconds = (now - created).total_seconds()
            if age_seconds > cfg.order_stale_seconds:
                stale.append(o["client_order_id"])
        except Exception as e:
            log.warning("Could not parse order timestamp: %s — %s", o["client_order_id"], e)
    return stale
