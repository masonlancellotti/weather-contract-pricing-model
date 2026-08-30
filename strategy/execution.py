"""
strategy/execution.py — Order execution with idempotency, deduplication, and stale cleanup.

Key safety rules:
  1. Every order gets a UUID client_order_id generated BEFORE submission
  2. client_order_id is checked against DB before submitting — never resend
  3. After placing, immediately persist to DB
  4. On reconnect/restart, re-read DB to restore awareness of open orders
  5. Stale orders are cancelled on each execution cycle
  6. Never place market orders
  7. Never average down
"""
from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from config import cfg
from models import Order, OrderStatus, Side, TradeSignal, Venue
from storage import DB
from strategy.risk import (
    clear_critical_venue_failures,
    detect_stale_orders,
    pre_flight_check,
    record_critical_venue_failure,
)

log = logging.getLogger("strategy.execution")

UTC = timezone.utc


def execute_signal(
    signal: TradeSignal,
    available_balance: Decimal,
    db: DB,
    kalshi_client=None,
    poly_client=None,
) -> Optional[Order]:
    """
    Attempt to place a limit order for a trade signal.
    Returns the Order on success, None on any failure or suppression.
    Does NOT raise — all errors are logged and return None.
    """
    # Pre-flight
    ok, reason = pre_flight_check(signal, available_balance, db)
    if not ok:
        log.info("Order suppressed: %s — %s", signal.market_id, reason)
        return None

    # Generate idempotency key
    client_order_id = str(uuid.uuid4())

    # Double-check DB — should not already have this key (it's a new UUID, but be paranoid)
    if db.get_order_by_client_id(client_order_id):
        log.error("UUID collision — this should never happen: %s", client_order_id)
        return None

    # Place order
    try:
        if signal.venue == Venue.KALSHI:
            if kalshi_client is None:
                log.error("Kalshi client not available")
                return None
            order = kalshi_client.place_order(
                ticker=signal.market_id,
                side=signal.side,
                contracts=signal.suggested_contracts,
                price_prob=signal.suggested_price,
                client_order_id=client_order_id,
            )
        elif signal.venue == Venue.POLYMARKET:
            if poly_client is None:
                log.error("Polymarket client not available")
                return None
            order = poly_client.place_order(
                token_id=signal.market_id,
                side=signal.side,
                contracts=signal.suggested_contracts,
                price_prob=signal.suggested_price,
                client_order_id=client_order_id,
            )
        else:
            log.error("Unknown venue: %s", signal.venue)
            return None

        # Persist immediately
        db.upsert_order(order)
        clear_critical_venue_failures(signal.venue)
        log.info(
            "ORDER PLACED venue=%s market=%s side=%s contracts=%d price=%.3f id=%s",
            order.venue.value, order.market_id, order.side.value,
            order.contracts, float(order.price), order.venue_order_id,
        )
        return order

    except Exception as e:
        log.error("Order placement failed: %s — %s", signal.market_id, e)
        record_critical_venue_failure(signal.venue, f"order_placement_failed:{e}")
        return None


def cancel_stale_orders(db: DB, kalshi_client=None, poly_client=None) -> int:
    """
    Cancel all stale open orders. Returns count cancelled.
    """
    stale_client_ids = detect_stale_orders(db)
    if not stale_client_ids:
        return 0

    cancelled = 0
    open_orders = {o["client_order_id"]: o for o in db.get_open_orders()}

    for client_id in stale_client_ids:
        o = open_orders.get(client_id)
        if not o:
            continue
        venue = Venue(o["venue"])
        venue_order_id = o["venue_order_id"]

        log.info("Cancelling stale order client=%s venue=%s venue_id=%s",
                 client_id, venue.value, venue_order_id)

        success = False
        try:
            if venue == Venue.KALSHI and kalshi_client:
                success = kalshi_client.cancel_order(venue_order_id)
            elif venue == Venue.POLYMARKET and poly_client:
                success = poly_client.cancel_order(venue_order_id)
        except Exception as e:
            log.warning("Cancel attempt failed for %s: %s", venue_order_id, e)
            record_critical_venue_failure(venue, f"cancel_failed:{e}")

        if success:
            db.mark_order_cancelled(client_id)
            cancelled += 1
        else:
            log.warning("Could not cancel order %s — will retry next cycle", venue_order_id)

    if cancelled:
        log.info("Cancelled %d stale orders", cancelled)
    return cancelled


def sync_orders_from_venue(
    db: DB,
    kalshi_client=None,
    poly_client=None,
    fill_sync_ok: Optional[dict[Venue, bool]] = None,
):
    """
    Reconcile local DB open orders against live venue state.
    Updates DB for any orders that have been filled or cancelled on the venue.
    Call on startup and periodically.
    """
    _sync_venue_orders(db, Venue.KALSHI, kalshi_client, fill_sync_ok)
    _sync_venue_orders(db, Venue.POLYMARKET, poly_client, fill_sync_ok)


def _sync_venue_orders(
    db: DB,
    venue: Venue,
    client,
    fill_sync_ok: Optional[dict[Venue, bool]] = None,
):
    if client is None:
        return
    if fill_sync_ok is not None and not fill_sync_ok.get(venue, False):
        log.warning("Skipping %s order reconciliation because fill sync failed", venue.value)
        return
    try:
        if venue == Venue.KALSHI:
            live_orders = {o["order_id"]: o for o in client.get_open_orders()}
        else:
            live_orders = {o.get("id", ""): o for o in client.get_open_orders()}

        local_open = db.get_open_orders(venue)
        for local in local_open:
            vid = local["venue_order_id"]
            if vid and vid not in live_orders:
                # Order is gone from venue — must have been filled or cancelled
                log.info("Order %s no longer open on %s — marking cancelled/filled",
                         vid, venue.value)
                filled_contracts, avg_fill_price = db.get_order_fill_stats(vid)
                if filled_contracts >= int(local["contracts"]):
                    db.mark_order_status(
                        local["client_order_id"],
                        OrderStatus.FILLED,
                        filled_contracts=filled_contracts,
                        fill_price=avg_fill_price,
                    )
                elif filled_contracts > 0:
                    db.mark_order_status(
                        local["client_order_id"],
                        OrderStatus.PARTIALLY_FILLED,
                        filled_contracts=filled_contracts,
                        fill_price=avg_fill_price,
                    )
                else:
                    db.mark_order_cancelled(local["client_order_id"])
                clear_critical_venue_failures(venue)
    except Exception as e:
        log.warning("Order sync failed for %s: %s", venue.value, e)
        record_critical_venue_failure(venue, f"order_sync_failed:{e}")
