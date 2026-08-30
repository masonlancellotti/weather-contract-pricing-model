from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

from data.kalshi_client import KalshiAPIError, KalshiClient
from data.kalshi_market_loader import KalshiMarketLoader, is_likely_weather_market
from data.storage import Storage
from parsing.market_parser import WeatherMarketParser

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistoricalLoadResult:
    markets: int = 0
    candlesticks: int = 0
    trades: int = 0
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "markets": self.markets,
            "candlesticks": self.candlesticks,
            "trades": self.trades,
            "warnings": list(self.warnings),
        }


class KalshiHistoricalLoader:
    """Load settled weather markets plus replayable candlesticks/trades."""

    def __init__(self, client: KalshiClient | None = None, storage: Storage | None = None):
        self.client = client or KalshiClient()
        self.storage = storage or Storage()
        self.parser = WeatherMarketParser()

    def cutoff(self) -> dict:
        return self.client.historical_cutoff()

    def load_history(
        self,
        start: date | None = None,
        end: date | None = None,
        limit: int = 100,
        market_ticker: str | None = None,
        weather_only: bool = True,
        period_interval: int = 60,
        include_trades: bool = True,
    ) -> HistoricalLoadResult:
        self.storage.init_db()
        warnings: list[str] = []
        start_ts, end_ts = _date_range_to_ts(start, end)
        markets = self._discover_markets(start_ts, end_ts, limit, market_ticker, weather_only, warnings)
        candles_saved = 0
        trades_saved = 0
        for market in markets:
            self.storage.save_market(market)
            contract = self.parser.parse(market)
            self.storage.save_parsed_contract(contract)
            candles = self._fetch_candlesticks(market, start_ts, end_ts, period_interval, warnings)
            for candle in candles:
                row = _normalize_candlestick(market, candle, period_interval)
                if row:
                    self.storage.upsert_historical_candlestick(row)
                    candles_saved += 1
            if include_trades:
                trades_saved += self._load_trades_for_market(str(market.get("ticker") or ""), start_ts, end_ts, warnings)
        return HistoricalLoadResult(markets=len(markets), candlesticks=candles_saved, trades=trades_saved, warnings=tuple(warnings))

    def historical_weather_markets(self, limit: int = 1000, cursor: str | None = None, **params) -> list[dict]:
        payload = self.client.get_historical_markets(limit=limit, cursor=cursor, **params)
        return [market for market in payload.get("markets", []) if is_likely_weather_market(market)]

    def _discover_markets(
        self,
        start_ts: int,
        end_ts: int,
        limit: int,
        market_ticker: str | None,
        weather_only: bool,
        warnings: list[str],
    ) -> list[dict]:
        if market_ticker:
            market = self._get_market_any_tier(market_ticker, warnings)
            return [market] if market else []

        markets: list[dict] = []
        seen: set[str] = set()
        if weather_only:
            for series in KalshiMarketLoader(client=self.client, storage=self.storage, parser=self.parser).discover_weather_series(max_series=max(limit, 25)):
                series_ticker = series.get("ticker")
                if not series_ticker:
                    continue
                for params in (
                    {"series_ticker": series_ticker, "status": "settled", "min_settled_ts": start_ts, "max_settled_ts": end_ts},
                    {"series_ticker": series_ticker, "status": "closed", "min_close_ts": start_ts, "max_close_ts": end_ts},
                ):
                    try:
                        payload = self.client.get_markets(limit=1000, **params)
                    except Exception as exc:
                        warnings.append(f"series {series_ticker} discovery failed: {exc}")
                        continue
                    for market in payload.get("markets", []):
                        ticker = str(market.get("ticker") or "")
                        if not ticker or ticker in seen or not is_likely_weather_market(market):
                            continue
                        contract = self.parser.parse(market)
                        if contract.variable_type not in {"high_temp", "low_temp"}:
                            continue
                        seen.add(ticker)
                        markets.append(market)
                        if len(markets) >= limit:
                            return markets

        params = {"status": "settled", "min_settled_ts": start_ts, "max_settled_ts": end_ts}
        for source, iterator in (
            ("live", lambda: self.client.iter_markets(limit=1000, max_pages=20, **params)),
            ("historical", lambda: self.client.iter_historical_markets(limit=1000, max_pages=20, min_settled_ts=start_ts, max_settled_ts=end_ts)),
        ):
            try:
                for market in iterator():
                    ticker = str(market.get("ticker") or "")
                    if not ticker or ticker in seen:
                        continue
                    if weather_only and not is_likely_weather_market(market):
                        continue
                    contract = self.parser.parse(market)
                    if weather_only and contract.variable_type not in {"high_temp", "low_temp"}:
                        continue
                    seen.add(ticker)
                    markets.append(market)
                    if len(markets) >= limit:
                        return markets
            except Exception as exc:
                message = f"{source} settled market discovery failed: {exc}"
                LOGGER.warning(message)
                warnings.append(message)
        return markets

    def _get_market_any_tier(self, ticker: str, warnings: list[str]) -> dict | None:
        try:
            payload = self.client.get_market(ticker)
            return payload.get("market", payload)
        except Exception as exc:
            warnings.append(f"live market lookup failed for {ticker}: {exc}")
        try:
            payload = self.client.get_historical_market(ticker)
            return payload.get("market", payload)
        except Exception as exc:
            warnings.append(f"historical market lookup failed for {ticker}: {exc}")
        return None

    def _fetch_candlesticks(self, market: dict, start_ts: int, end_ts: int, period_interval: int, warnings: list[str]) -> list[dict]:
        ticker = str(market.get("ticker") or "")
        series_ticker = str(market.get("series_ticker") or market.get("event_ticker") or "").split("-")[0]
        if not ticker:
            return []
        errors: list[str] = []
        if series_ticker:
            try:
                payload = self.client.get_market_candlesticks(series_ticker, ticker, start_ts, end_ts, period_interval)
                return payload.get("candlesticks", [])
            except Exception as exc:
                errors.append(f"live candlesticks failed for {ticker}: {exc}")
        try:
            payload = self.client.get_historical_market_candlesticks(ticker, start_ts, end_ts, period_interval)
            return payload.get("candlesticks", [])
        except Exception as exc:
            errors.append(f"historical candlesticks failed for {ticker}: {exc}")
        warnings.extend(errors)
        return []

    def _load_trades_for_market(self, ticker: str, start_ts: int, end_ts: int, warnings: list[str]) -> int:
        if not ticker:
            return 0
        saved = 0
        errors: list[str] = []
        for source, iterator in (
            ("live", lambda: self.client.iter_trades(ticker=ticker, min_ts=start_ts, max_ts=end_ts, limit=1000)),
            ("historical", lambda: self.client.iter_historical_trades(ticker=ticker, min_ts=start_ts, max_ts=end_ts, limit=1000)),
        ):
            try:
                for trade in iterator():
                    row = _normalize_trade(trade, ticker)
                    if row:
                        self.storage.insert_historical_trade(row)
                        saved += 1
            except Exception as exc:
                errors.append(f"{source} trades failed for {ticker}: {exc}")
        warnings.extend(errors[:2])
        return saved


def _date_range_to_ts(start: date | None, end: date | None) -> tuple[int, int]:
    start_dt = datetime.combine(start or date(2025, 1, 1), time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end or datetime.now(timezone.utc).date(), time.max, tzinfo=timezone.utc)
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def _normalize_candlestick(market: dict, candle: dict[str, Any], period_interval: int) -> dict | None:
    ts = _timestamp_from_candle(candle)
    if ts is None:
        return None
    yes_bid = _price_close(candle.get("yes_bid"))
    yes_ask = _price_close(candle.get("yes_ask"))
    price = candle.get("price") or {}
    close_yes = _price_close(price)
    if close_yes is None:
        close_yes = yes_bid
    return {
        "market_ticker": str(market.get("ticker") or candle.get("ticker") or ""),
        "ts": ts,
        "period": str(period_interval),
        "open_yes_price": _price_key(price, "open"),
        "high_yes_price": _price_key(price, "high"),
        "low_yes_price": _price_key(price, "low"),
        "close_yes_price": close_yes,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": 100.0 - yes_ask if yes_ask is not None else None,
        "no_ask": 100.0 - yes_bid if yes_bid is not None else None,
        "volume": _float(candle.get("volume_fp") or candle.get("volume")),
        "open_interest": _float(candle.get("open_interest_fp") or candle.get("open_interest")),
        "raw_json": json.dumps(candle, default=str),
    }


def _normalize_trade(trade: dict[str, Any], default_ticker: str) -> dict | None:
    ts = _parse_trade_ts(trade)
    if ts is None:
        return None
    yes_price = _price_value(trade.get("yes_price_dollars") or trade.get("yes_price"))
    no_price = _price_value(trade.get("no_price_dollars") or trade.get("no_price"))
    price = yes_price if yes_price is not None else (100.0 - no_price if no_price is not None else None)
    return {
        "market_ticker": str(trade.get("ticker") or trade.get("market_ticker") or default_ticker),
        "ts": ts,
        "price": price,
        "count": _float(trade.get("count_fp") or trade.get("count")),
        "yes_price": yes_price,
        "no_price": no_price,
        "side": trade.get("taker_side") or trade.get("side"),
        "raw_json": json.dumps(trade, default=str),
    }


def _timestamp_from_candle(candle: dict[str, Any]) -> datetime | None:
    raw = candle.get("end_period_ts") or candle.get("ts")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _parse_trade_ts(trade: dict[str, Any]) -> datetime | None:
    raw = trade.get("created_time") or trade.get("ts")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _price_close(node: Any) -> float | None:
    return _price_key(node, "close")


def _price_key(node: Any, key: str) -> float | None:
    if not isinstance(node, dict):
        return _price_value(node)
    return _price_value(node.get(f"{key}_dollars") or node.get(key))


def _price_value(value: Any) -> float | None:
    raw = _float(value)
    if raw is None:
        return None
    return raw * 100.0 if 0 <= raw <= 1 else raw


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
