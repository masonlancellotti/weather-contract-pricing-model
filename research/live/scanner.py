from __future__ import annotations

import logging
from datetime import datetime, timezone

from backtest.execution import NormalizedOrderBook
from data.kalshi_client import KalshiClient
from data.kalshi_market_loader import KalshiMarketLoader
from data.storage import Storage
from data.weather_client import WeatherClient
from data.weather_station_mapper import StationMapper
from features.feature_builder import FeatureBuilder
from models.baseline_model import HeuristicWeatherModel
from parsing.market_parser import WeatherMarketParser
from strategies.already_hit_threshold import AlreadyHitThresholdStrategy
from strategies.base import TradeSignal
from strategies.ladder_consistency import LadderConsistencyStrategy
from strategies.late_day_high_fade import LateDayHighFadeStrategy
from live.risk_manager import RiskManager

LOGGER = logging.getLogger(__name__)


class LiveScanner:
    def __init__(
        self,
        client: KalshiClient | None = None,
        storage: Storage | None = None,
        weather_client: WeatherClient | None = None,
    ):
        self.client = client or KalshiClient()
        self.storage = storage or Storage()
        self.weather_client = weather_client or WeatherClient()
        self.parser = WeatherMarketParser()
        self.mapper = StationMapper()
        self.features = FeatureBuilder()
        self.model = HeuristicWeatherModel()
        self.risk = RiskManager()
        self.single_market_strategies = [AlreadyHitThresholdStrategy(), LateDayHighFadeStrategy()]
        self.ladder = LadderConsistencyStrategy()

    def scan_once(self, max_markets: int = 50) -> list[dict]:
        self.storage.init_db()
        markets = KalshiMarketLoader(client=self.client, storage=self.storage, parser=self.parser).load_active_weather_markets(
            persist=False,
            max_pages=1,
            max_series=max(1, min(25, max_markets)),
        )
        rows: list[dict] = []
        contracts = []
        features_by_ticker: dict[str, dict] = {}
        for market in markets[:max_markets]:
            ticker = market.get("ticker")
            if not ticker:
                continue
            contract = self.parser.parse(market)
            contracts.append(contract)
            self.storage.save_market(market)
            self.storage.save_parsed_contract(contract)
            mapping = self.mapper.resolve(contract.city, contract.station_code)
            if not mapping or not contract.local_date:
                row = _row(contract.market_ticker, "SKIP", contract.not_tradable_reason() or "missing station/date", contract, {}, None)
                rows.append(row)
                self.storage.save_signal(TradeSignal(contract.market_ticker, "scanner", "SKIP", reason=row["skip_reason"], skip_reason=row["skip_reason"]))
                continue
            raw_book = self.client.get_orderbook(ticker, depth=10)
            book = NormalizedOrderBook.from_kalshi(ticker, raw_book)
            self.storage.insert_json("orderbook_snapshots", raw_book, ticker=ticker, snapshot_time=datetime.now(timezone.utc))
            weather_state = self.weather_client.weather_state(mapping, contract.local_date)
            feature_row = self.features.build(contract, market, weather_state, mapping, book)
            features_by_ticker[ticker] = feature_row
            prediction = self.model.predict(contract, feature_row)
            best_signal = self._best_signal(contract, feature_row, prediction)
            risk = self.risk.evaluate(best_signal, feature_row)
            row = _row(ticker, risk.action, risk.reason, contract, feature_row, prediction, best_signal)
            rows.append(row)
            self.storage.save_signal(best_signal)
            self.storage.insert_json("model_predictions", prediction.to_dict(), market_ticker=ticker, prediction_time=datetime.now(timezone.utc), model_version=prediction.model_version)
        for signal in self.ladder.generate_group(contracts, features_by_ticker):
            self.storage.save_signal(signal)
            rows.append({"market_ticker": signal.market_ticker, "action": "PAPER", "strategy": signal.strategy, "reason": signal.reason, "edge_cents": signal.edge_cents})
        return rows

    def _best_signal(self, contract, features, prediction) -> TradeSignal:
        signals = [strategy.generate(contract, features, prediction) for strategy in self.single_market_strategies]
        actionable = [s for s in signals if s.action not in {"SKIP", "WATCH"}]
        if not actionable:
            return max(signals, key=lambda s: s.confidence)
        return max(actionable, key=lambda s: s.edge_cents or 0.0)


def _row(ticker: str, action: str, risk_reason: str, contract, features: dict, prediction, signal: TradeSignal | None = None) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_ticker": ticker,
        "city": contract.city,
        "station_code": contract.station_code,
        "local_date": str(contract.local_date),
        "threshold": contract.threshold,
        "variable_type": contract.variable_type,
        "yes_bid": features.get("yes_bid"),
        "yes_ask": features.get("yes_ask"),
        "no_bid": features.get("no_bid"),
        "no_ask": features.get("no_ask"),
        "spread": features.get("spread"),
        "fair_value": prediction.fair_value_cents if prediction else None,
        "edge_cents": signal.edge_cents if signal else None,
        "strategy": signal.strategy if signal else "scanner",
        "action": action,
        "confidence": prediction.confidence if prediction else contract.parse_confidence,
        "reason": signal.reason if signal else risk_reason,
        "skip_reason": risk_reason,
        "parse_warnings": "; ".join(contract.warnings),
    }
