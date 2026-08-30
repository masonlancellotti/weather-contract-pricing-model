"""
venues/kalshi.py — Kalshi venue adapter.

Auth: RSA-PSS SHA-256 signing.
  Message = timestamp_ms_str + HTTP_METHOD + path_without_querystring
  Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE (base64)

Uses the official kalshi_python_sync SDK where it reduces code.
Falls back to raw httpx for calls not well-covered by the SDK.

All prices in this adapter are in CENTS (1–99 integer) for Kalshi's API,
and are converted to/from 0-1 Decimal at the boundary.
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid
from decimal import Decimal
from typing import Optional, AsyncGenerator

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import websockets

from config import cfg
from models import (
    Balance, Fill, MarketSnapshot, Order, OrderStatus, Position, Side, Venue
)
from utils.time import utcnow, utcnow_ms

log = logging.getLogger("kalshi")

PUBLIC_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
BASE_URL = cfg.kalshi_base_url
WS_URL = cfg.kalshi_ws_url


def _load_private_key():
    pem = cfg.load_kalshi_private_key()
    return serialization.load_pem_private_key(pem.encode(), password=None)


def _sign(method: str, path: str) -> dict[str, str]:
    """Return the three Kalshi auth headers for a request."""
    ts_ms = utcnow_ms()
    msg = f"{ts_ms}{method}{path}".encode()
    private_key = _load_private_key()
    sig = private_key.sign(msg, padding.PSS(
        mgf=padding.MGF1(hashes.SHA256()),
        salt_length=padding.PSS.DIGEST_LENGTH,
    ), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY": cfg.kalshi_api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


def _headers(method: str, path: str) -> dict[str, str]:
    h = _sign(method, path)
    h["Content-Type"] = "application/json"
    return h


def _path(endpoint: str) -> str:
    """Strip base URL to get just the path for signing."""
    return "/trade-api/v2" + endpoint


class KalshiClient:
    """
    Synchronous Kalshi REST client.
    All public methods raise on HTTP errors after logging.
    """

    def __init__(self):
        self._http = httpx.Client(
            base_url=BASE_URL,
            timeout=15.0,
        )

    def close(self):
        self._http.close()

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        path = _path(endpoint)
        r = self._http.get(
            endpoint,
            headers=_headers("GET", path),
            params=params or {},
        )
        r.raise_for_status()
        return r.json()

    def _post(self, endpoint: str, body: dict) -> dict:
        path = _path(endpoint)
        r = self._http.post(
            endpoint,
            headers=_headers("POST", path),
            content=json.dumps(body),
        )
        r.raise_for_status()
        return r.json()

    def _delete(self, endpoint: str, body: dict | None = None) -> dict:
        path = _path(endpoint)
        r = self._http.request(
            "DELETE",
            endpoint,
            headers=_headers("DELETE", path),
            content=json.dumps(body) if body else None,
        )
        r.raise_for_status()
        return r.json()

    # ── Account ──────────────────────────────────────────────────────────────────

    def get_balance(self) -> Balance:
        data = self._get("/portfolio/balance")
        # balance returned in cents
        avail = Decimal(str(data["balance"])) / 100
        total = Decimal(str(data.get("portfolio_value", data["balance"]))) / 100
        return Balance(venue=Venue.KALSHI, available=avail, total=total)

    def get_positions(self) -> list[Position]:
        data = self._get("/portfolio/positions")
        positions = []
        for mp in data.get("market_positions", []):
            if mp.get("position_fp", 0) == 0:
                continue
            contracts = int(mp["position_fp"])
            side = Side.YES if contracts > 0 else Side.NO
            positions.append(Position(
                venue=Venue.KALSHI,
                market_id=mp["ticker"],
                city=_ticker_to_city(mp["ticker"]),
                bucket_label=mp.get("ticker", ""),
                side=side,
                contracts=abs(contracts),
                avg_price=Decimal(str(mp.get("market_exposure_fp", 0))) / max(abs(contracts), 1) / 100,
            ))
        return positions

    def get_fills(self, min_ts: Optional[int] = None) -> list[Fill]:
        params: dict = {"limit": 100}
        if min_ts:
            params["min_ts"] = min_ts
        data = self._get("/portfolio/fills", params=params)
        fills = []
        for f in data.get("fills", []):
            fills.append(Fill(
                venue=Venue.KALSHI,
                fill_id=f["fill_id"],
                order_id=f["order_id"],
                market_id=f["ticker"],
                side=Side(f["side"]),
                contracts=int(f.get("count_fp", 0)),
                price=Decimal(str(f.get("yes_price_dollars", 0))),
                fee=Decimal(str(f.get("fee_cost", 0))) / 100,
                ts=utcnow(),
            ))
        return fills

    def get_open_orders(self) -> list[dict]:
        data = self._get("/portfolio/orders", params={"status": "resting"})
        return data.get("orders", [])

    # ── Market discovery ─────────────────────────────────────────────────────────

    def get_weather_markets(
        self,
        series_ticker: str,
        city_code: str,
        settlement_date: str,
    ) -> list[MarketSnapshot]:
        """
        Discover active temperature markets for a city on a settlement date.
        Uses public market data, then filters to the actual ticker date.
        """
        try:
            r = httpx.get(
                f"https://api.elections.kalshi.com/trade-api/v2/markets",
                params={"series_ticker": series_ticker, "status": "open", "limit": 100},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("markets"):
                log.debug("Kalshi sample market: %s", data["markets"][0])
            else:
                log.warning(
                    "Kalshi series %s returned no open markets for %s (%s)",
                    series_ticker,
                    city_code,
                    settlement_date,
                )
        except httpx.HTTPStatusError as e:
            log.warning(
                "Kalshi market lookup failed for %s (%s): %s. "
                "This often means the mapped series ticker is stale.",
                city_code,
                series_ticker,
                e,
            )
            return []

        snapshots = []
        for market in data.get("markets", []):
            ticker = market.get("ticker", "")
            market_date = _ticker_date_to_iso(ticker)
            if market_date != settlement_date:
                continue

            snap = _market_to_snapshot(market, city_code, market_date)
            if snap:
                snapshots.append(snap)

        return snapshots

    def get_orderbook(self, ticker: str) -> dict:
        """Returns {yes: [[price_cents, qty], ...], no: [...]}"""
        data = self._get(f"/markets/{ticker}/orderbook")
        return data.get("orderbook", {})

    # ── Orders ───────────────────────────────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        side: Side,
        contracts: int,
        price_prob: Decimal,   # 0-1
        client_order_id: str,
    ) -> Order:
        """Place a limit order. price_prob is 0-1; we convert to cents."""
        yes_price_cents = int(round(float(price_prob) * 100))
        yes_price_cents = max(1, min(99, yes_price_cents))

        # Determine action: buying YES at yes_price means action=buy, side=yes
        # Buying NO at no_price means we want YES at (100 - no_price)
        if side == Side.YES:
            api_side = "yes"
            api_price = yes_price_cents
        else:
            api_side = "no"
            api_price = 100 - yes_price_cents  # no_price = 100 - yes_price

        body = {
            "ticker": ticker,
            "side": api_side,
            "action": "buy",
            "type": "limit",
            "count": contracts,
            "yes_price": api_price,
            "client_order_id": client_order_id,
        }
        log.info("Kalshi place_order ticker=%s side=%s contracts=%d price_cents=%d",
                 ticker, api_side, contracts, api_price)
        data = self._post("/portfolio/orders", body)
        raw = data.get("order", {})
        return _raw_order_to_order(raw, client_order_id)

    def cancel_order(self, order_id: str) -> bool:
        try:
            path = f"/portfolio/orders/{order_id}"
            p = _path(path)
            r = self._http.request(
                "DELETE",
                path,
                headers=_headers("DELETE", p),
            )
            r.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            log.warning("Cancel order %s failed: %s", order_id, e)
            return False

    def cancel_orders_batch(self, order_ids: list[str]) -> int:
        if not order_ids:
            return 0
        try:
            body = {"orders": [{"order_id": oid} for oid in order_ids]}
            self._delete("/portfolio/orders/batched", body)
            return len(order_ids)
        except httpx.HTTPStatusError as e:
            log.warning("Batch cancel failed: %s", e)
            return 0


# ── WebSocket streaming ───────────────────────────────────────────────────────

async def kalshi_ws_stream(tickers: list[str], on_message) -> None:
    """
    Connects to Kalshi WS, subscribes to orderbook + fill channels,
    and calls on_message(msg_dict) for each incoming message.
    Auto-reconnects on disconnect.
    """
    while True:
        try:
            ts_ms = utcnow_ms()
            path = "/trade-api/ws/v2"
            private_key = _load_private_key()
            msg_to_sign = f"{ts_ms}GET{path}".encode()
            sig = private_key.sign(
                msg_to_sign,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
            extra_headers = {
                "KALSHI-ACCESS-KEY": cfg.kalshi_api_key_id,
                "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            }

            async with websockets.connect(
                WS_URL,
                additional_headers=extra_headers,
                ping_interval=20,
                ping_timeout=20,
            ) as ws:
                log.info("Kalshi WS connected")

                # Subscribe to orderbook deltas
                sub_msg = {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": tickers,
                    },
                }
                await ws.send(json.dumps(sub_msg))

                # Subscribe to fills
                fill_sub = {
                    "id": 2,
                    "cmd": "subscribe",
                    "params": {"channels": ["fill"]},
                }
                await ws.send(json.dumps(fill_sub))

                async for raw in ws:
                    msg = json.loads(raw)
                    try:
                        await on_message(msg)
                    except Exception as e:
                        log.exception("Error handling Kalshi WS message: %s", e)

        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            log.warning("Kalshi WS disconnected: %s — reconnecting in 5s", e)
            await asyncio.sleep(5)
        except Exception as e:
            log.exception("Kalshi WS unexpected error: %s", e)
            await asyncio.sleep(10)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _ticker_to_city(ticker: str) -> str:
    """Extract city code from a Kalshi weather ticker."""
    mapping = {
        "KXHIGHNY": "NYC", "KXHIGHCHI": "CHI", "KXHIGHATL": "ATL",
        "KXHIGHLA": "LA", "KXHIGHDAL": "DAL", "KXHIGHHOU": "HOU",
        "KXHIGHPHX": "PHX", "KXHIGHSF": "SF", "KXHIGHSEA": "SEA",
        "KXHIGHDEN": "DEN", "KXHIGHBOS": "BOS", "KXHIGHMIA": "MIA",
        "KXHIGHTATL": "ATL", "KXHIGHTBOS": "BOS", "KXHIGHTDAL": "DAL",
        "KXHIGHTHOU": "HOU", "KXHIGHTPHX": "PHX", "KXHIGHTSEA": "SEA",
        "KXHIGHTSFO": "SF", "KXHIGHLAX": "LA",
        "KXHIGHAUS": "AUS", "KXHIGHTDC": "DC", "KXHIGHPHIL": "PHIL",
        "KXHIGHTLV": "LV", "KXHIGHTMIN": "MIN", "KXHIGHTOKC": "OKC",
        "KXHIGHTSATX": "SATX",
    }
    for prefix, city in mapping.items():
        if ticker.startswith(prefix):
            return city
    return ticker[:8]


def _market_to_snapshot(market: dict, city_code: str, settlement_date: str) -> Optional[MarketSnapshot]:
    try:
        ticker = market["ticker"]
        subtitle = market.get("subtitle") or market.get("yes_sub_title") or ""

        bucket_min, bucket_max = _parse_bucket(subtitle)

        yes_bid = (
            Decimal(str(market["yes_bid_dollars"]))
            if market.get("yes_bid_dollars") not in (None, "")
            else None
        )
        yes_ask = (
            Decimal(str(market["yes_ask_dollars"]))
            if market.get("yes_ask_dollars") not in (None, "")
            else None
        )
        last = (
            Decimal(str(market["last_price_dollars"]))
            if market.get("last_price_dollars") not in (None, "")
            else None
        )

        return MarketSnapshot(
            venue=Venue.KALSHI,
            market_id=ticker,
            city=_CITY_CODE_MAP.get(city_code, city_code),
            bucket_label=subtitle,
            bucket_min=bucket_min,
            bucket_max=bucket_max,
            settlement_date=settlement_date,
            best_bid=yes_bid,
            best_ask=yes_ask,
            last_price=last,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            is_open=(market.get("status") == "active"),
        )
    except Exception as e:
        log.warning("Could not parse Kalshi market snapshot: %s — %s", market.get("ticker"), e)
        return None


import re

def _parse_bucket(subtitle: str) -> tuple[Optional[float], Optional[float]]:
    s = subtitle.strip()

    m = re.match(r"(\d+(?:\.\d+)?)°?\s*to\s*(\d+(?:\.\d+)?)°?", s, re.IGNORECASE)
    if m:
        return float(m.group(1)), float(m.group(2))

    m = re.match(r"(\d+(?:\.\d+)?)°?\s*-\s*(\d+(?:\.\d+)?)°?", s, re.IGNORECASE)
    if m:
        return float(m.group(1)), float(m.group(2))

    m = re.match(r"(\d+(?:\.\d+)?)°?\s*or\s*below", s, re.IGNORECASE)
    if m:
        return None, float(m.group(1))

    m = re.match(r"(\d+(?:\.\d+)?)°?\s*or\s*above", s, re.IGNORECASE)
    if m:
        return float(m.group(1)), None

    m = re.match(r"[≥>]=?\s*(\d+(?:\.\d+)?)", s)
    if m:
        return float(m.group(1)), None

    m = re.match(r"[<≤]=?\s*(\d+(?:\.\d+)?)", s)
    if m:
        return None, float(m.group(1))

    return None, None


def _raw_order_to_order(raw: dict, client_order_id: str) -> Order:
    return Order(
        venue=Venue.KALSHI,
        venue_order_id=raw.get("order_id", ""),
        client_order_id=client_order_id,
        market_id=raw.get("ticker", ""),
        city=_ticker_to_city(raw.get("ticker", "")),
        bucket_label=raw.get("ticker", ""),
        side=Side(raw.get("side", "yes")),
        price=Decimal(str(raw.get("yes_price", 0))) / 100,
        contracts=int(raw.get("remaining_count", raw.get("count", 0))),
        status=_map_status(raw.get("status", "resting")),
        created_at=utcnow(),
        updated_at=utcnow(),
    )


def _map_status(s: str) -> OrderStatus:
    return {
        "resting": OrderStatus.OPEN,
        "executed": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELLED,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
    }.get(s, OrderStatus.OPEN)


_CITY_CODE_MAP = {
    "NY": "NYC", "CHI": "CHI", "ATL": "ATL", "LA": "LA",
    "DAL": "DAL", "HOU": "HOU", "PHX": "PHX", "SF": "SF",
    "SEA": "SEA", "DEN": "DEN", "BOS": "BOS", "MIA": "MIA",
}

def _ticker_date_to_iso(ticker: str) -> str:
    """
    Example:
    KXHIGHNY-26APR22-B57.5 -> 2026-04-22
    """
    import re
    from datetime import datetime

    m = re.search(r"-(\d{2}[A-Z]{3}\d{2})-", ticker)
    if not m:
        return ""

    try:
        dt = datetime.strptime(m.group(1), "%y%b%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return ""
