from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from config import Settings, settings

LOGGER = logging.getLogger(__name__)


class KalshiAPIError(RuntimeError):
    pass


class KalshiClient:
    """Small Kalshi REST client for public data plus disabled-by-default auth support."""

    def __init__(self, cfg: Settings = settings, session: requests.Session | None = None):
        self.cfg = cfg
        self.base_url = cfg.kalshi_base_url.rstrip("/")
        self.session = session or requests.Session()
        self.cache_dir = cfg.cache_dir / "kalshi"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._private_key = None
        self.stats: dict[str, Any] = {
            "total_429s": 0,
            "total_retries": 0,
            "endpoint_429_counts": {},
        }

    def get_markets(self, **params: Any) -> dict:
        return self.get("/markets", params={k: v for k, v in params.items() if v is not None})

    def iter_markets(self, limit: int = 1000, max_pages: int | None = None, **params: Any):
        cursor: str | None = None
        pages = 0
        while True:
            payload = self.get_markets(limit=limit, cursor=cursor, **params)
            for market in payload.get("markets", []):
                yield market
            cursor = payload.get("cursor") or None
            pages += 1
            if not cursor or (max_pages and pages >= max_pages):
                break

    def get_market(self, ticker: str) -> dict:
        return self.get(f"/markets/{ticker}")

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self.get(f"/markets/{ticker}/orderbook", params={"depth": depth})

    def get_multiple_orderbooks(self, tickers: list[str]) -> dict:
        if not tickers:
            return {"orderbooks": []}
        return self.get("/markets/orderbooks", params={"tickers": tickers})

    def get_trades(self, ticker: str | None = None, min_ts: int | None = None, max_ts: int | None = None, limit: int = 1000) -> dict:
        return self.get(
            "/markets/trades",
            params={"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": limit},
        )

    def iter_trades(self, ticker: str | None = None, min_ts: int | None = None, max_ts: int | None = None, limit: int = 1000):
        cursor: str | None = None
        while True:
            payload = self.get(
                "/markets/trades",
                params={"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": limit, "cursor": cursor},
            )
            for trade in payload.get("trades", []):
                yield trade
            cursor = payload.get("cursor") or None
            if not cursor:
                break

    def get_events(self, with_nested_markets: bool = False, **params: Any) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        return self.get("/events", params=params)

    def get_series_list(self, **params: Any) -> dict:
        return self.get("/series", params={k: v for k, v in params.items() if v is not None})

    def historical_cutoff(self) -> dict:
        return self.get("/historical/cutoff")

    def get_historical_markets(self, **params: Any) -> dict:
        return self.get("/historical/markets", params={k: v for k, v in params.items() if v is not None})

    def get_historical_market(self, ticker: str) -> dict:
        return self.get(f"/historical/markets/{ticker}")

    def iter_historical_markets(self, limit: int = 1000, max_pages: int | None = None, **params: Any):
        cursor: str | None = None
        pages = 0
        while True:
            payload = self.get_historical_markets(limit=limit, cursor=cursor, **params)
            for market in payload.get("markets", []):
                yield market
            cursor = payload.get("cursor") or None
            pages += 1
            if not cursor or (max_pages and pages >= max_pages):
                break

    def get_market_candlesticks(
        self,
        series_ticker: str,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
        include_latest_before_start: bool = False,
    ) -> dict:
        return self.get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params={
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
                "include_latest_before_start": str(include_latest_before_start).lower(),
            },
        )

    def get_historical_market_candlesticks(
        self,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
    ) -> dict:
        return self.get(
            f"/historical/markets/{ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )

    def get_historical_trades(self, ticker: str | None = None, min_ts: int | None = None, max_ts: int | None = None, limit: int = 1000, cursor: str | None = None) -> dict:
        return self.get(
            "/historical/trades",
            params={"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": limit, "cursor": cursor},
        )

    def iter_historical_trades(self, ticker: str | None = None, min_ts: int | None = None, max_ts: int | None = None, limit: int = 1000):
        cursor: str | None = None
        while True:
            payload = self.get_historical_trades(ticker=ticker, min_ts=min_ts, max_ts=max_ts, limit=limit, cursor=cursor)
            for trade in payload.get("trades", []):
                yield trade
            cursor = payload.get("cursor") or None
            if not cursor:
                break

    def get(self, endpoint: str, params: dict[str, Any] | None = None, auth: bool = False, use_cache: bool = False) -> dict:
        return self.request("GET", endpoint, params=params, auth=auth, use_cache=use_cache)

    def request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_body: dict | None = None,
        auth: bool = False,
        use_cache: bool = False,
    ) -> dict:
        endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        clean_params = _clean_params(params or {})
        cache_key = self._cache_key(method, endpoint, clean_params, json_body)
        if use_cache:
            cached = self._read_cache(cache_key)
            if cached is not None:
                return cached

        url = f"{self.base_url}{endpoint}"
        headers = self._headers(method, endpoint, auth)
        last_error: Exception | None = None
        for attempt in range(max(1, self.cfg.kalshi_max_retries)):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=clean_params,
                    json=json_body,
                    headers=headers,
                    timeout=20,
                )
                if response.status_code in {429, 500, 502, 503, 504}:
                    if response.status_code == 429:
                        self.stats["total_429s"] += 1
                        counts = self.stats["endpoint_429_counts"]
                        counts[endpoint] = counts.get(endpoint, 0) + 1
                    self.stats["total_retries"] += 1
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                    exponential = min(2.0 * (2**attempt), float(self.cfg.kalshi_backoff_max_seconds))
                    sleep_seconds = retry_after if retry_after is not None else exponential + random.uniform(0.0, min(1.0, exponential * 0.25))
                    LOGGER.warning("Kalshi transient status %s on %s; sleeping %.1fs", response.status_code, endpoint, sleep_seconds)
                    time.sleep(sleep_seconds)
                    continue
                if response.status_code >= 400:
                    raise KalshiAPIError(f"Kalshi {response.status_code} {endpoint}: {response.text[:500]}")
                payload = response.json()
                self._write_cache(cache_key, payload)
                return payload
            except (requests.RequestException, ValueError, KalshiAPIError) as exc:
                last_error = exc
                if isinstance(exc, KalshiAPIError):
                    raise
                self.stats["total_retries"] += 1
                sleep_seconds = min(1.5 * (2**attempt), float(self.cfg.kalshi_backoff_max_seconds)) + random.uniform(0.0, 1.0)
                time.sleep(sleep_seconds)
        raise KalshiAPIError(f"Kalshi request failed after retries: {endpoint}") from last_error

    def _headers(self, method: str, endpoint: str, auth: bool) -> dict[str, str]:
        if not auth:
            return {"Accept": "application/json"}
        if not self.cfg.kalshi_api_key_id or not self.cfg.kalshi_private_key_path:
            raise KalshiAPIError("Authenticated Kalshi request requested without API credentials.")
        timestamp = str(int(time.time() * 1000))
        path_for_signature = "/trade-api/v2" + endpoint.split("?")[0]
        message = timestamp + method.upper() + path_for_signature
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.cfg.kalshi_api_key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(message),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def _sign(self, message: str) -> str:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:  # pragma: no cover
            raise KalshiAPIError("Install cryptography to use authenticated Kalshi APIs.") from exc

        if self._private_key is None:
            key_path = Path(self.cfg.kalshi_private_key_path or "")
            with key_path.open("rb") as fh:
                self._private_key = serialization.load_pem_private_key(fh.read(), password=None)
        signature = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _cache_key(self, method: str, endpoint: str, params: dict[str, Any], json_body: dict | None) -> str:
        canonical = json.dumps({"m": method, "e": endpoint, "p": params, "j": json_body}, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _read_cache(self, key: str) -> dict | None:
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _write_cache(self, key: str, payload: dict) -> None:
        path = self.cache_dir / f"{key}.json"
        path.write_text(json.dumps(payload, default=str), encoding="utf-8")


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, list):
            # Kalshi accepts comma-separated ticker lists in REST docs for /markets.
            clean[key] = ",".join(map(str, value))
        else:
            clean[key] = value
    return clean


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
