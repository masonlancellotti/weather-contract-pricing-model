"""
strategy/sizing.py -- Risk-aware position sizing for trade signals.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Optional

from config import cfg
from models import MarketSnapshot, ModelOutput, TradeSignal
from storage import DB
from utils.time import hours_until_entry_cutoff


def size_signal(
    signal: TradeSignal,
    snapshot: MarketSnapshot,
    model: ModelOutput,
    db: DB,
) -> Optional[TradeSignal]:
    """
    Scale contract count by edge quality, confidence, time remaining, and exposure capacity.
    Returns the updated signal or None if the remaining budget is too small to trade safely.
    """
    remaining_venue = max(Decimal("0"), cfg.max_venue_exposure_usd - db.get_total_exposure(signal.venue))
    remaining_city = max(Decimal("0"), cfg.max_city_exposure_usd - db.get_city_exposure(signal.venue, signal.city))
    bucket_cap = min(cfg.max_order_size_usd, cfg.max_city_exposure_usd / 2)
    remaining_market = max(
        Decimal("0"),
        bucket_cap - db.get_market_side_exposure(signal.venue, signal.market_id, signal.side),
    )
    budget = min(cfg.max_order_size_usd, remaining_venue, remaining_city, remaining_market)
    if budget < cfg.min_order_notional_usd:
        return None

    spread_score = _spread_score(snapshot)
    edge_score = _clamp(signal.edge / 0.25, 0.0, 1.0)
    confidence_score = _clamp(model.confidence, 0.0, 1.0)
    time_score = _clamp(hours_until_entry_cutoff(signal.city) / 8.0, 0.0, 1.0)
    risk_score = _clamp(
        0.35 * edge_score + 0.30 * confidence_score + 0.20 * spread_score + 0.15 * time_score,
        0.0,
        1.0,
    )
    sized_budget = budget * Decimal(str(risk_score))
    if sized_budget < cfg.min_order_notional_usd:
        return None

    price = max(signal.suggested_price, Decimal("0.01"))
    contracts = int((sized_budget / price).to_integral_value(rounding=ROUND_DOWN))
    contracts = max(0, min(contracts, cfg.max_contracts_per_order))
    if contracts < 1:
        return None

    notional = price * contracts
    if notional < cfg.min_order_notional_usd:
        return None

    signal.suggested_contracts = contracts
    signal.reason = (
        f"{signal.reason} risk_score={risk_score:.2f}"
        f" budget={float(budget):.2f} sized={float(notional):.2f}"
    )
    return signal


def _spread_score(snapshot: MarketSnapshot) -> float:
    if snapshot.best_bid is None or snapshot.best_ask is None:
        return 0.35
    spread = float(snapshot.best_ask - snapshot.best_bid)
    return _clamp(1.0 - (spread / 0.15), 0.0, 1.0)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
