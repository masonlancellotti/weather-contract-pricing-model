from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from data.kalshi_client import KalshiClient
from data.storage import Storage
from parsing.market_parser import WeatherMarketParser

LOGGER = logging.getLogger(__name__)

WEATHER_KEYWORDS = (
    "temperature",
    "highest temperature",
    "lowest temperature",
    "high temp",
    "low temp",
    "weather",
    "rain",
    "snow",
    "precip",
    "wind",
)

WEATHER_SERIES_HINTS = ("KXHIGH", "KXLOW", "KXTEMP", "KXRAIN", "KXSNOW", "KXWIND")


class KalshiMarketLoader:
    def __init__(self, client: KalshiClient | None = None, storage: Storage | None = None, parser: WeatherMarketParser | None = None):
        self.client = client or KalshiClient()
        self.storage = storage or Storage()
        self.parser = parser or WeatherMarketParser()

    def load_active_weather_markets(self, persist: bool = True, max_pages: int | None = None, max_series: int | None = None) -> list[dict]:
        markets: list[dict] = []
        seen: set[str] = set()

        for series in self.discover_weather_series(max_series=max_series):
            series_ticker = series.get("ticker")
            if not series_ticker:
                continue
            try:
                payload = self.client.get_markets(series_ticker=series_ticker, status="open", limit=1000)
            except Exception as exc:
                LOGGER.warning("Failed loading markets for weather series %s: %s", series_ticker, exc)
                continue
            for market in payload.get("markets", []):
                market_ticker = str(market.get("ticker") or "")
                if market_ticker and market_ticker not in seen:
                    seen.add(market_ticker)
                    markets.append(market)

        # Fallback: broad scanning catches new series before series metadata is
        # refreshed, but weather will not necessarily appear in the first page.
        for market in self.client.iter_markets(status="open", limit=1000, max_pages=max_pages):
            market_ticker = str(market.get("ticker") or "")
            if market_ticker and market_ticker not in seen and is_likely_weather_market(market):
                seen.add(market_ticker)
                markets.append(market)

        weather_markets = [market for market in markets if is_likely_weather_market(market)]
        if persist:
            self.storage.init_db()
            for market in weather_markets:
                self.storage.save_market(market)
                self.storage.insert_json(
                    "market_snapshots",
                    market,
                    ticker=market.get("ticker"),
                    snapshot_time=datetime.now(timezone.utc),
                )
                self.storage.save_parsed_contract(self.parser.parse(market))
        LOGGER.info("Loaded %d likely weather markets from %d open markets", len(weather_markets), len(markets))
        return weather_markets

    def discover_weather_series(self, max_series: int | None = None) -> list[dict]:
        series: list[dict] = []
        try:
            payload = self.client.get_series_list(
                category="Climate and Weather",
                include_product_metadata="true",
                include_volume="true",
            )
            series.extend(payload.get("series", []))
        except Exception as exc:
            LOGGER.warning("Weather category series discovery failed: %s", exc)

        if not series:
            payload = self.client.get_series_list(include_product_metadata="true", include_volume="true")
            series.extend(item for item in payload.get("series", []) if is_likely_weather_series(item))

        deduped: dict[str, dict] = {}
        for item in series:
            if is_likely_weather_series(item):
                deduped[str(item.get("ticker"))] = item
        values = list(deduped.values())
        values.sort(key=lambda item: float(item.get("volume_fp") or 0.0), reverse=True)
        return values[:max_series] if max_series else values


def is_likely_weather_market(market: dict) -> bool:
    text = " ".join(
        str(market.get(key) or "")
        for key in (
            "ticker",
            "event_ticker",
            "series_ticker",
            "category",
            "title",
            "subtitle",
            "yes_sub_title",
            "rules_primary",
            "rules_secondary",
        )
    ).lower()
    if any(hint.lower() in text for hint in WEATHER_SERIES_HINTS):
        return True
    return any(keyword in text for keyword in WEATHER_KEYWORDS)


def is_likely_weather_series(series: dict) -> bool:
    text = " ".join(
        str(series.get(key) or "")
        for key in ("ticker", "title", "category", "tags", "settlement_sources")
    ).lower()
    if "climate and weather" in text:
        return True
    if any(hint.lower() in text for hint in WEATHER_SERIES_HINTS):
        return True
    return any(keyword in text for keyword in WEATHER_KEYWORDS)
