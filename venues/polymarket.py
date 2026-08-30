"""
venues/polymarket.py — Polymarket CLOB venue adapter.

Auth model:
  L1: EIP-712 wallet signing (creates API credentials once)
  L2: HMAC-SHA256 (POLY_API_KEY/SECRET/PASSPHRASE) for all trading requests
  Orders still require EIP-712 signing even with L2 auth.

Uses py-clob-client SDK where it handles signing complexity.
Direct httpx for lightweight calls that don't need the SDK.

IMPORTANT: Polymarket uses Polygon (chain 137). USDC.e is the collateral token.
           Prices are 0.0–1.0 decimals (NOT cents).
           A REST heartbeat POST to /heartbeat must be sent every <10s or
           all open orders on that API key get cancelled automatically.

This adapter does NOT support Polymarket US (polymarket.us) — that's a
completely separate CFTC-regulated platform with different auth (Ed25519).
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Optional

import httpx
import websockets

from config import cfg
from models import (
    Balance, Fill, MarketSnapshot, Order, OrderStatus, Position, Side, Venue
)
from utils.time import utcnow

log = logging.getLogger("polymarket")


IDENTIFIER_SUPPORT_COMPLETE = False


def live_trading_enabled() -> bool:
    """Polymarket live trading remains disabled until token identifiers are modeled correctly."""
    return cfg.poly_live_trading_enabled and IDENTIFIER_SUPPORT_COMPLETE

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"
WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

HEARTBEAT_INTERVAL = 8  # seconds — must be < 10


class PolymarketClient:
    """
    Polymarket CLOB client wrapping py-clob-client.
    All public methods raise on errors after logging.
    """

    def __init__(self):
        self._client = None
        self._init_client()

    def _init_client(self):
        """Initialize the py-clob-client. Creates API creds if not set."""
        if not cfg.has_poly_creds():
            log.warning("No Polymarket private key configured — adapter disabled")
            return
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            if cfg.poly_api_key and cfg.poly_api_secret and cfg.poly_api_passphrase:
                creds = ApiCreds(
                    api_key=cfg.poly_api_key,
                    api_secret=cfg.poly_api_secret,
                    api_passphrase=cfg.poly_api_passphrase,
                )
                self._client = ClobClient(
                    host=CLOB_BASE,
                    chain_id=cfg.poly_chain_id,
                    key=cfg.poly_private_key,
                    creds=creds,
                )
                log.info("Polymarket client initialized with existing API creds")
            else:
                # Create new API credentials via L1 auth
                self._client = ClobClient(
                    host=CLOB_BASE,
                    chain_id=cfg.poly_chain_id,
                    key=cfg.poly_private_key,
                )
                creds = self._client.create_or_derive_api_creds()
                self._client.set_api_creds(creds)
                log.info("Polymarket API creds created — save these to .env")
                # Print plaintext once to stdout so user can capture it
                print(f"\n>>> POLY_API_KEY={creds.api_key}")
                print(f">>> POLY_API_SECRET={creds.api_secret}")
                print(f">>> POLY_API_PASSPHRASE={creds.api_passphrase}\n")
        except Exception as e:
            log.error("Polymarket client init failed: %s", e)
            self._client = None

    @property
    def active(self) -> bool:
        return self._client is not None

    def _require_client(self):
        if not self._client:
            raise RuntimeError("Polymarket client not initialized")

    # ── Account ──────────────────────────────────────────────────────────────────

    def get_balance(self) -> Balance:
        self._require_client()
        try:
            resp = self._client.get_balance_allowance(asset_type="COLLATERAL")
            # Returns 6-decimal fixed-point USDC
            raw = Decimal(str(resp.get("balance", 0)))
            avail = raw / Decimal("1000000")
            return Balance(venue=Venue.POLYMARKET, available=avail, total=avail)
        except Exception as e:
            log.error("Polymarket get_balance failed: %s", e)
            raise

    def get_positions(self, address: str) -> list[Position]:
        """
        Positions are fetched from the public data API by wallet address.
        address = your Polygon wallet address (0x...)
        """
        try:
            r = httpx.get(f"{DATA_API_BASE}/positions", params={"user": address}, timeout=10)
            r.raise_for_status()
            positions = []
            for p in r.json():
                size = float(p.get("size", 0))
                if size == 0:
                    continue
                positions.append(Position(
                    venue=Venue.POLYMARKET,
                    market_id=p.get("conditionId", ""),
                    city=_slug_to_city(p.get("market", "")),
                    bucket_label=p.get("outcome", ""),
                    side=Side.YES if p.get("outcome", "").upper() in ("YES", "TRUE") else Side.NO,
                    contracts=int(size),
                    avg_price=Decimal(str(p.get("avgPrice", 0))),
                ))
            return positions
        except Exception as e:
            log.error("Polymarket get_positions failed: %s", e)
            return []

    def get_fills(self, address: str, limit: int = 100) -> list[Fill]:
        try:
            r = httpx.get(
                f"{DATA_API_BASE}/trades",
                params={"user": address, "limit": limit},
                timeout=10,
            )
            r.raise_for_status()
            fills = []
            for t in r.json():
                fills.append(Fill(
                    venue=Venue.POLYMARKET,
                    fill_id=t.get("id", ""),
                    order_id=t.get("orderId", ""),
                    market_id=t.get("conditionId", ""),
                    side=Side.YES if t.get("outcome", "").upper() in ("YES", "TRUE") else Side.NO,
                    contracts=int(float(t.get("size", 0))),
                    price=Decimal(str(t.get("price", 0))),
                    fee=Decimal("0"),  # Polymarket embeds fee in price
                    ts=utcnow(),
                ))
            return fills
        except Exception as e:
            log.error("Polymarket get_fills failed: %s", e)
            return []

    def get_open_orders(self) -> list[dict]:
        self._require_client()
        try:
            resp = self._client.get_orders()
            if isinstance(resp, list):
                return resp
            if isinstance(resp, dict):
                return resp.get("data", []) or resp.get("orders", [])
            return []
        except Exception as e:
            log.error("Polymarket get_open_orders failed: %s", e)
            return []

    # ── Market discovery ─────────────────────────────────────────────────────────

    def get_weather_markets(self, city: str, settlement_date: str) -> list[MarketSnapshot]:
        """
        Discover active temperature markets for a city via Gamma API.
        Returns MarketSnapshot list.
        """
        try:
            r = httpx.get(
                f"{GAMMA_BASE}/events",
                params={
                    "q": f"temperature {_city_to_search_term(city)}",
                    "active": "true",
                    "closed": "false",
                    "limit": 50,
                },
                timeout=15,
            )
            r.raise_for_status()
            events = r.json()
            snapshots = []
            for event in events:
                event_date = event.get("endDate", "")[:10]
                if event_date and event_date != settlement_date:
                    continue
                for market in event.get("markets", []):
                    snap = _gamma_market_to_snapshot(market, city, settlement_date)
                    if snap:
                        snapshots.append(snap)
            return snapshots
        except Exception as e:
            log.error("Polymarket get_weather_markets failed: %s", e)
            return []

    def get_orderbook(self, token_id: str) -> dict:
        self._require_client()
        try:
            return self._client.get_order_book(token_id)
        except Exception as e:
            log.error("Polymarket get_orderbook %s failed: %s", token_id, e)
            return {}

    # ── Orders ───────────────────────────────────────────────────────────────────

    def place_order(
        self,
        token_id: str,
        side: Side,
        contracts: int,
        price_prob: Decimal,   # 0-1
        client_order_id: str,  # used as salt for dedup
    ) -> Order:
        """
        Place a GTC limit order.
        token_id = YES token ID for the market.
        side = YES means buy YES token; NO means buy NO token (sell YES at complementary price).
        """
        if not live_trading_enabled():
            raise RuntimeError("Polymarket live trading disabled until token identifiers are fixed")
        self._require_client()
        from py_clob_client.clob_types import OrderArgs, OrderType

        price_f = float(price_prob)
        # Polymarket prices are 0.0-1.0; for NO side, we buy NO token at (1 - price)
        if side == Side.NO:
            # NO token has its own token ID — we'd need it separately.
            # Simplification: we only trade YES side for v1 (buy or sell YES token).
            # TODO: get NO token ID and handle separately.
            raise NotImplementedError("NO side requires separate NO token ID — use YES side pricing")

        order_args = OrderArgs(
            token_id=token_id,
            price=round(price_f, 4),
            size=float(contracts),
            side="BUY",
            fee_rate_bps=0,
        )
        log.info("Polymarket place_order token=%s side=YES contracts=%d price=%.4f",
                 token_id[:16], contracts, price_f)
        try:
            signed = self._client.create_and_post_order(order_args)
            raw_id = signed.get("orderID", "")
            return Order(
                venue=Venue.POLYMARKET,
                venue_order_id=raw_id,
                client_order_id=client_order_id,
                market_id=token_id,
                city="",
                bucket_label="",
                side=Side.YES,
                price=price_prob,
                contracts=contracts,
                status=OrderStatus.OPEN,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        except Exception as e:
            log.error("Polymarket place_order failed: %s", e)
            raise

    def cancel_order(self, order_id: str) -> bool:
        self._require_client()
        try:
            self._client.cancel(order_id)
            return True
        except Exception as e:
            log.warning("Polymarket cancel_order %s failed: %s", order_id, e)
            return False

    def cancel_all_orders(self) -> bool:
        self._require_client()
        try:
            self._client.cancel_all()
            return True
        except Exception as e:
            log.warning("Polymarket cancel_all failed: %s", e)
            return False

    def send_heartbeat(self) -> bool:
        """
        CRITICAL: Must call this every <10 seconds or all open orders are cancelled.
        Call in a background loop when you have active open orders.
        """
        if not self._client:
            return False
        try:
            self._client.post_heartbeat()
            return True
        except Exception as e:
            log.warning("Polymarket heartbeat failed: %s", e)
            return False


# ── WebSocket streaming ───────────────────────────────────────────────────────

async def polymarket_ws_stream(token_ids: list[str], on_message) -> None:
    """
    Public market channel WS stream for price/orderbook updates.
    token_ids = list of YES token IDs to subscribe to.
    PING/PONG every 10s is required.
    """
    while True:
        try:
            async with websockets.connect(
                WS_MARKET_URL,
                ping_interval=8,
                ping_timeout=10,
            ) as ws:
                log.info("Polymarket WS (market) connected")

                sub = {
                    "auth": {},
                    "type": "market",
                    "assets_ids": token_ids,
                }
                await ws.send(json.dumps(sub))

                async for raw in ws:
                    try:
                        msgs = json.loads(raw)
                        if isinstance(msgs, list):
                            for msg in msgs:
                                await on_message(msg)
                        else:
                            await on_message(msgs)
                    except Exception as e:
                        log.exception("Error handling Polymarket WS message: %s", e)

        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            log.warning("Polymarket WS disconnected: %s — reconnecting in 5s", e)
            await asyncio.sleep(5)
        except Exception as e:
            log.exception("Polymarket WS unexpected error: %s", e)
            await asyncio.sleep(10)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _slug_to_city(slug: str) -> str:
    slug_lower = slug.lower()
    for city, terms in {
        "NYC": ["new-york", "nyc"],
        "CHI": ["chicago", "chi"],
        "LA": ["los-angeles", "la-"],
        "DAL": ["dallas"],
        "ATL": ["atlanta"],
        "BOS": ["boston"],
        "MIA": ["miami"],
        "SEA": ["seattle"],
        "SF": ["san-francisco"],
        "DEN": ["denver"],
        "PHX": ["phoenix"],
        "HOU": ["houston"],
    }.items():
        if any(t in slug_lower for t in terms):
            return city
    return ""


def _city_to_search_term(city: str) -> str:
    return {
        "NYC": "New York",
        "CHI": "Chicago",
        "LA": "Los Angeles",
        "DAL": "Dallas",
        "ATL": "Atlanta",
        "BOS": "Boston",
        "MIA": "Miami",
        "SEA": "Seattle",
        "SF": "San Francisco",
        "DEN": "Denver",
        "PHX": "Phoenix",
        "HOU": "Houston",
    }.get(city, city)


def _gamma_market_to_snapshot(market: dict, city: str, settlement_date: str) -> Optional[MarketSnapshot]:
    try:
        cid = market.get("conditionId", "")
        if not cid:
            return None
        outcomes = market.get("outcomes", "[]")
        if isinstance(outcomes, str):
            import json as _json
            outcomes = _json.loads(outcomes)
        prices = market.get("outcomePrices", "[]")
        if isinstance(prices, str):
            import json as _json
            prices = _json.loads(prices)

        yes_price = Decimal(str(prices[0])) if len(prices) > 0 else None
        question = market.get("question", "")
        bucket_min, bucket_max = _parse_poly_bucket(question)

        return MarketSnapshot(
            venue=Venue.POLYMARKET,
            market_id=cid,
            city=city,
            bucket_label=question,
            bucket_min=bucket_min,
            bucket_max=bucket_max,
            settlement_date=settlement_date,
            best_bid=yes_price * Decimal("0.99") if yes_price else None,
            best_ask=yes_price * Decimal("1.01") if yes_price else None,
            last_price=yes_price,
            yes_bid=None,
            yes_ask=None,
            is_open=market.get("active", False),
        )
    except Exception as e:
        log.warning("Could not parse Polymarket market snapshot: %s — %s", market.get("conditionId"), e)
        return None


def _parse_poly_bucket(question: str) -> tuple[Optional[float], Optional[float]]:
    """Parse temperature range from Polymarket question text."""
    import re
    # "Will the high be above 72°F" or ">= 72"
    m = re.search(r"(?:above|>=|≥|over)\s+(\d+)", question, re.IGNORECASE)
    if m:
        return float(m.group(1)), None
    m = re.search(r"(?:below|<=|≤|under)\s+(\d+)", question, re.IGNORECASE)
    if m:
        return None, float(m.group(1))
    m = re.search(r"(\d+)\s*(?:to|-|–)\s*(\d+)", question)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None
