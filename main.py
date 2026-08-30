"""
main.py — Single-process entry point.

Architecture:
  - One asyncio event loop
  - Two WS tasks (Kalshi + Polymarket) for live market data
  - One scheduler for periodic work: weather refresh, strategy tick, stale cleanup
  - Signal handling for graceful shutdown
  - All state in SQLite

Run: python main.py
"""
from __future__ import annotations
import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

from config import cfg
from models import MarketSnapshot, Venue
from storage import db
from strategy.engine import StrategyEngine
from strategy.risk import is_safe_mode, set_safe_mode
from utils.logging import setup_logging, add_db_handler, set_db
from utils.time import settlement_date_for_city
from weather.mapping import active_cities

log = logging.getLogger("main")

UTC = timezone.utc


def init_clients():
    """Initialize venue clients. Returns (kalshi, poly)."""
    kalshi = None
    poly = None

    if cfg.has_kalshi_creds():
        try:
            from venues.kalshi import KalshiClient
            kalshi = KalshiClient()
            log.info("Kalshi client initialized (demo=%s)", cfg.kalshi_demo)
        except Exception as e:
            log.error("Kalshi client init failed: %s", e)
    else:
        log.warning("No Kalshi credentials — Kalshi adapter disabled")

    if cfg.has_poly_creds():
        try:
            from venues.polymarket import PolymarketClient, live_trading_enabled
            poly = PolymarketClient()
            if poly.active:
                if live_trading_enabled():
                    log.info("Polymarket client initialized")
                else:
                    log.warning(
                        "Polymarket client initialized in read-only safety mode "
                        "(live trading disabled until token identifiers are fixed)"
                    )
            else:
                log.warning("Polymarket client inactive")
                poly = None
        except Exception as e:
            log.error("Polymarket client init failed: %s", e)
    else:
        log.warning("No Polymarket credentials — Polymarket adapter disabled")

    return kalshi, poly


def refresh_markets(kalshi, poly) -> None:
    """
    Discover active weather markets and store snapshots.
    Called once on startup and periodically.
    """
    for city_meta in active_cities():
        settlement_date = settlement_date_for_city(city_meta.city_code)
        log.debug("Refreshing markets for %s %s", city_meta.city_code, settlement_date)

        # Kalshi
        if kalshi:
            try:
                snaps = kalshi.get_weather_markets(
                    city_meta.kalshi_series, city_meta.city_code, settlement_date
                )
                for s in snaps:
                    db.upsert_market_snapshot(s)
                log.info("Kalshi: found %d markets for %s %s",
                         len(snaps), city_meta.city_code, settlement_date)
            except Exception as e:
                log.warning("Kalshi market refresh failed for %s: %s", city_meta.city_code, e)

        # Polymarket
        if poly:
            try:
                snaps = poly.get_weather_markets(city_meta.city_code, settlement_date)
                for s in snaps:
                    db.upsert_market_snapshot(s)
                log.info("Polymarket: found %d markets for %s %s",
                         len(snaps), city_meta.city_code, settlement_date)
            except Exception as e:
                log.warning("Polymarket market refresh failed for %s: %s", city_meta.city_code, e)


def sync_fills_and_positions(kalshi, poly) -> dict[Venue, bool]:
    """Sync fills and positions from both venues into DB."""
    result = {Venue.KALSHI: False, Venue.POLYMARKET: False}
    if kalshi:
        try:
            fills = kalshi.get_fills()
            for f in fills:
                db.insert_fill(f)
            positions = kalshi.get_positions()
            for p in positions:
                db.upsert_position(p)
            result[Venue.KALSHI] = True
        except Exception as e:
            log.warning("Kalshi fill/position sync failed: %s", e)

    if poly and poly.active:
        try:
            from web3 import Web3
            # Derive address from private key
            w3 = Web3()
            account = w3.eth.account.from_key(cfg.poly_private_key)
            address = account.address

            fills = poly.get_fills(address)
            for f in fills:
                db.insert_fill(f)
            positions = poly.get_positions(address)
            for p in positions:
                db.upsert_position(p)
            result[Venue.POLYMARKET] = True
        except Exception as e:
            log.warning("Polymarket fill/position sync failed: %s", e)
    return result


# ── Kalshi WebSocket handler ──────────────────────────────────────────────────

async def handle_kalshi_ws_message(msg: dict, db) -> None:
    """Process incoming Kalshi WS messages and update market snapshots."""
    msg_type = msg.get("type")
    if msg_type in ("orderbook_snapshot", "orderbook_delta"):
        seq = msg.get("seq", 0)
        market_ticker = msg.get("market_ticker", "")
        yes_side = msg.get("yes", [])
        no_side = msg.get("no", [])

        best_bid = None
        best_ask = None
        if yes_side:
            # yes_side = [[price_cents, qty], ...] sorted desc
            best_bid = Decimal(str(yes_side[0][0])) / 100
        if no_side:
            # no_side prices are NO prices; YES ask = 100 - best_no_bid
            best_no_bid = no_side[0][0]
            best_ask = Decimal(str(100 - best_no_bid)) / 100

        if market_ticker and (best_bid or best_ask):
            # Find existing snapshot and update price
            from models import MarketSnapshot as MS
            # Quick upsert with just price update
            db.conn.execute(
                """UPDATE market_snapshots SET best_bid=?, best_ask=?, ts=?
                   WHERE market_id=? AND ts = (
                     SELECT MAX(ts) FROM market_snapshots WHERE market_id=?
                   )""",
                (
                    str(best_bid) if best_bid else None,
                    str(best_ask) if best_ask else None,
                    datetime.now(UTC).isoformat(),
                    market_ticker,
                    market_ticker,
                ),
            )
            db.conn.commit()

    elif msg_type == "fill":
        # Real-time fill notification — log it
        log.info("Kalshi fill: %s", json.dumps(msg))


# ── Polymarket WebSocket handler ──────────────────────────────────────────────

async def handle_poly_ws_message(msg: dict, db) -> None:
    msg_type = msg.get("event_type", msg.get("type", ""))
    if msg_type in ("book", "best_bid_ask", "price_change"):
        asset_id = msg.get("asset_id", "")
        best_bid = msg.get("best_bid")
        best_ask = msg.get("best_ask")
        if asset_id and (best_bid is not None or best_ask is not None):
            db.conn.execute(
                """UPDATE market_snapshots SET best_bid=?, best_ask=?, ts=?
                   WHERE market_id=? AND ts = (
                     SELECT MAX(ts) FROM market_snapshots WHERE market_id=?
                   )""",
                (
                    str(best_bid) if best_bid else None,
                    str(best_ask) if best_ask else None,
                    datetime.now(UTC).isoformat(),
                    asset_id,
                    asset_id,
                ),
            )
            db.conn.commit()
    elif msg_type in ("trade", "last_trade_price"):
        log.debug("Polymarket trade: %s", json.dumps(msg))


# ── Heartbeat loop ────────────────────────────────────────────────────────────

async def polymarket_heartbeat_loop(poly) -> None:
    """
    Send Polymarket heartbeat every 8s while there are open orders.
    CRITICAL: Polymarket cancels all open orders if heartbeat not sent within 10s.
    """
    while True:
        await asyncio.sleep(8)
        if poly and poly.active:
            open_orders = db.get_open_orders(Venue.POLYMARKET)
            if open_orders:
                poly.send_heartbeat()


# ── Periodic scheduler ────────────────────────────────────────────────────────

async def periodic_scheduler(engine: StrategyEngine, kalshi, poly, stop_event, executor) -> None:
    """Runs periodic tasks on their own cadence."""
    market_refresh_interval = 600    # 10 min — market list rarely changes
    strategy_interval = cfg.market_poll_seconds
    weather_interval = cfg.weather_refresh_seconds
    fill_sync_interval = 60

    last_market = 0
    last_strategy = 0
    last_weather = 0
    last_fill_sync = 0

    loop = asyncio.get_running_loop()

    while not stop_event.is_set():
        now = loop.time()

        if now - last_market > market_refresh_interval and not stop_event.is_set():
            log.debug("Refreshing market list")
            await loop.run_in_executor(
                executor, refresh_markets, kalshi, poly
            )
            last_market = now

        if now - last_fill_sync > fill_sync_interval and not stop_event.is_set():
            fill_sync_ok = await loop.run_in_executor(
                executor, sync_fills_and_positions, kalshi, poly
            )
            from strategy.execution import sync_orders_from_venue
            await loop.run_in_executor(
                executor, sync_orders_from_venue, db, kalshi, poly, fill_sync_ok
            )
            last_fill_sync = now

        if now - last_strategy > strategy_interval and not stop_event.is_set():
            if not is_safe_mode() and not cfg.kill_switch:
                await loop.run_in_executor(
                    executor, engine.tick
                )
            last_strategy = now

        await asyncio.sleep(5)


async def main() -> None:
    setup_logging(cfg.log_level)
    db.connect()
    set_db(db)
    add_db_handler()

    log.info("=" * 60)
    log.info("Weather Trader starting up")
    log.info("LIVE_ENABLED=%s KILL_SWITCH=%s", cfg.live_enabled, cfg.kill_switch)
    log.info("Active cities: %s", cfg.active_cities)
    log.info("=" * 60)

    if not cfg.live_enabled:
        log.warning("⚠️  Paper mode — orders will be logged but not submitted")

    # Initialize clients
    kalshi, poly = init_clients()

    # Bootstrap: initial market refresh
    refresh_markets(kalshi, poly)
    fill_sync_ok = sync_fills_and_positions(kalshi, poly)

    # Sync local DB orders against live venues
    from strategy.execution import sync_orders_from_venue
    sync_orders_from_venue(db, kalshi, poly, fill_sync_ok)

    engine = StrategyEngine(db, kalshi, poly)

    # Collect active market tickers for WS subscriptions
    kalshi_tickers = _get_active_kalshi_tickers()
    poly_token_ids = _get_active_poly_tokens()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    executor = ThreadPoolExecutor(max_workers=4)

    tasks = []

    # Kalshi WS stream
    if kalshi and kalshi_tickers:
        from venues.kalshi import kalshi_ws_stream
        async def _kalshi_handler(msg):
            await handle_kalshi_ws_message(msg, db)
        tasks.append(asyncio.create_task(
            kalshi_ws_stream(kalshi_tickers, _kalshi_handler),
            name="kalshi_ws"
        ))
        log.info("Kalshi WS subscribed to %d tickers", len(kalshi_tickers))

    # Polymarket WS stream
    if poly and poly_token_ids:
        from venues.polymarket import polymarket_ws_stream
        async def _poly_handler(msg):
            await handle_poly_ws_message(msg, db)
        tasks.append(asyncio.create_task(
            polymarket_ws_stream(poly_token_ids, _poly_handler),
            name="poly_ws"
        ))
        log.info("Polymarket WS subscribed to %d tokens", len(poly_token_ids))

    # Polymarket heartbeat
    if poly and poly.active:
        tasks.append(asyncio.create_task(
            polymarket_heartbeat_loop(poly),
            name="poly_heartbeat"
        ))

    # Periodic scheduler
    tasks.append(asyncio.create_task(
        periodic_scheduler(engine, kalshi, poly, stop_event, executor),
        name="scheduler"
    ))

    log.info("All tasks started. Bot is running. Ctrl+C to stop.")

    # Graceful shutdown

    def _shutdown(signum, frame):
        log.info("Shutdown signal received")
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    await stop_event.wait()

    log.info("Shutting down — cancelling open orders...")
    # Cancel all resting orders on shutdown
    if kalshi:
        try:
            open_orders = db.get_open_orders(Venue.KALSHI)
            venue_ids = [o["venue_order_id"] for o in open_orders if o["venue_order_id"]]
            if venue_ids:
                kalshi.cancel_orders_batch(venue_ids)
                log.info("Cancelled %d Kalshi orders", len(venue_ids))
        except Exception as e:
            log.error("Shutdown Kalshi cancel failed: %s", e)

    if poly and poly.active:
        try:
            poly.cancel_all_orders()
            log.info("Cancelled all Polymarket orders")
        except Exception as e:
            log.error("Shutdown Polymarket cancel failed: %s", e)

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    executor.shutdown(wait=True, cancel_futures=True)

    db.close()
    log.info("Shutdown complete")


def _get_active_kalshi_tickers() -> list[str]:
    rows = db.conn.execute(
        "SELECT DISTINCT market_id FROM market_snapshots WHERE venue='kalshi' AND is_open=1"
    ).fetchall()
    return [r["market_id"] for r in rows]


def _get_active_poly_tokens() -> list[str]:
    from venues.polymarket import live_trading_enabled

    if not live_trading_enabled():
        return []
    rows = db.conn.execute(
        "SELECT DISTINCT market_id FROM market_snapshots WHERE venue='polymarket' AND is_open=1"
    ).fetchall()
    return [r["market_id"] for r in rows]


if __name__ == "__main__":
    asyncio.run(main())
