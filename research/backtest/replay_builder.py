from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from data.storage import Storage
from data.weather_client import WeatherClient, WeatherObservation
from data.weather_station_mapper import StationMapper
from parsing.weather_contract import WeatherContract


@dataclass(frozen=True)
class ReplayBuildResult:
    markets: int
    snapshots: int
    skipped: int
    warnings: list[str]

    def to_dict(self) -> dict:
        return {"markets": self.markets, "snapshots": self.snapshots, "skipped": self.skipped, "warnings": self.warnings}


class ReplayBuilder:
    def __init__(self, storage: Storage | None = None, weather_client: WeatherClient | None = None):
        self.storage = storage or Storage()
        self.weather_client = weather_client or WeatherClient()
        self.mapper = StationMapper()

    def build(self, start: date | None = None, end: date | None = None, market_ticker: str | None = None) -> ReplayBuildResult:
        self.storage.init_db()
        contracts = self._contracts(start, end, market_ticker)
        snapshots = 0
        skipped = 0
        warnings: list[str] = []
        for contract in contracts:
            candles = self._candles(contract.market_ticker, start, end)
            if candles.empty:
                skipped += 1
                warnings.append(f"{contract.market_ticker}: missing historical candlesticks")
                continue
            label = self._label(contract.market_ticker)
            if label is None or label.get("yes_result") is None:
                skipped += 1
                warnings.append(f"{contract.market_ticker}: missing settlement label")
                continue
            mapping = self.mapper.resolve(contract.city, contract.station_code)
            if mapping is None or contract.local_date is None:
                skipped += 1
                warnings.append(f"{contract.market_ticker}: missing station mapping/date")
                continue
            observations = self.weather_client.historical_hourly_observations(mapping.station_code, contract.local_date, mapping.timezone)
            for _, candle in candles.sort_values("ts").iterrows():
                ts = _parse_dt(candle["ts"])
                if ts is None:
                    continue
                weather_features = _weather_features_asof(contract, observations, mapping.timezone, ts)
                market_features = _market_features(contract, candle, ts)
                self.storage.upsert_replay_snapshot(
                    {
                        "market_ticker": contract.market_ticker,
                        "ts": ts,
                        "yes_bid": market_features["yes_bid"],
                        "yes_ask": market_features["yes_ask"],
                        "yes_mid": market_features["yes_mid"],
                        "no_bid": market_features["no_bid"],
                        "no_ask": market_features["no_ask"],
                        "last_trade_price": market_features["last_trade_price"],
                        "volume": market_features["volume"],
                        "open_interest": market_features["open_interest"],
                        "weather_features_json": json.dumps(weather_features, default=str),
                        "market_features_json": json.dumps(market_features, default=str),
                        "replay_data_type": "historical_candlestick",
                        "full_orderbook_json": None,
                    }
                )
                snapshots += 1
        return ReplayBuildResult(markets=len(contracts), snapshots=snapshots, skipped=skipped, warnings=warnings[:50])

    def build_from_live_orderbooks(self, start: date | None = None, end: date | None = None, market_ticker: str | None = None) -> ReplayBuildResult:
        self.storage.init_db()
        contracts = self._contracts(None, None, market_ticker)
        snapshots = 0
        skipped = 0
        warnings: list[str] = []
        for ticker, contract in contracts_by_ticker(contracts).items():
            if market_ticker and ticker != market_ticker:
                continue
            rows = self._live_orderbooks(ticker, start, end)
            if rows.empty:
                skipped += 1
                continue
            if self._label(ticker) is None:
                skipped += 1
                warnings.append(f"{ticker}: no settlement label yet; live orderbook replay waits for resolution")
                continue
            mapping = self.mapper.resolve(contract.city, contract.station_code)
            if mapping is None or contract.local_date is None:
                skipped += 1
                continue
            observations = self.weather_client.historical_hourly_observations(mapping.station_code, contract.local_date, mapping.timezone)
            for _, row in rows.sort_values("ts").iterrows():
                ts = _parse_dt(row["ts"])
                if ts is None:
                    continue
                weather_features = _weather_features_asof(contract, observations, mapping.timezone, ts)
                market_features = {
                    "yes_bid": _num(row.get("yes_best_bid")),
                    "yes_ask": _num(row.get("yes_best_ask")),
                    "yes_mid": _num(row.get("mid_cents")),
                    "no_bid": _num(row.get("no_best_bid")),
                    "no_ask": _num(row.get("no_best_ask")),
                    "spread": _num(row.get("spread_cents")),
                    "last_trade_price": None,
                    "volume": None,
                    "open_interest": None,
                    "time_to_close_minutes": (contract.close_time - ts).total_seconds() / 60.0 if contract.close_time else None,
                    "time_to_expiration_minutes": (contract.expiration_time - ts).total_seconds() / 60.0 if contract.expiration_time else None,
                    "replay_data_type": "recorded_full_orderbook",
                }
                self.storage.upsert_replay_snapshot(
                    {
                        "market_ticker": ticker,
                        "ts": ts,
                        "yes_bid": market_features["yes_bid"],
                        "yes_ask": market_features["yes_ask"],
                        "yes_mid": market_features["yes_mid"],
                        "no_bid": market_features["no_bid"],
                        "no_ask": market_features["no_ask"],
                        "last_trade_price": None,
                        "volume": None,
                        "open_interest": None,
                        "weather_features_json": json.dumps(weather_features, default=str),
                        "market_features_json": json.dumps(market_features, default=str),
                        "replay_data_type": "recorded_full_orderbook",
                        "full_orderbook_json": row.get("raw_json"),
                    }
                )
                snapshots += 1
        return ReplayBuildResult(markets=len(contracts), snapshots=snapshots, skipped=skipped, warnings=warnings[:50])

    def _contracts(self, start: date | None, end: date | None, market_ticker: str | None) -> list[WeatherContract]:
        frame = self.storage.fetch_table("parsed_contracts", limit=100000)
        if frame.empty:
            return []
        contracts: list[WeatherContract] = []
        seen: set[str] = set()
        for _, row in frame.sort_values("id", ascending=False).iterrows():
            payload = row["payload"]
            if not isinstance(payload, dict):
                continue
            contract = WeatherContract.model_validate(payload)
            if contract.market_ticker in seen:
                continue
            seen.add(contract.market_ticker)
            if market_ticker and contract.market_ticker != market_ticker:
                continue
            if contract.local_date is None or contract.variable_type not in {"high_temp", "low_temp"}:
                continue
            if start and contract.local_date < start:
                continue
            if end and contract.local_date > end:
                continue
            contracts.append(contract)
        return contracts

    def _candles(self, ticker: str, start: date | None, end: date | None):
        clauses = ["market_ticker = :ticker"]
        params = {"ticker": ticker}
        if start:
            clauses.append("date(ts) >= :start")
            params["start"] = start.isoformat()
        if end:
            clauses.append("date(ts) <= :end")
            params["end"] = end.isoformat()
        return self.storage.fetch_sql(
            f"SELECT * FROM historical_candlesticks WHERE {' AND '.join(clauses)} ORDER BY ts",
            params,
        )

    def _label(self, ticker: str) -> dict | None:
        frame = self.storage.fetch_sql("SELECT * FROM settlement_labels WHERE market_ticker = :ticker LIMIT 1", {"ticker": ticker})
        if frame.empty:
            return None
        return frame.iloc[0].to_dict()

    def _live_orderbooks(self, ticker: str, start: date | None, end: date | None):
        clauses = ["market_ticker = :ticker"]
        params = {"ticker": ticker}
        if start:
            clauses.append("date(ts) >= :start")
            params["start"] = start.isoformat()
        if end:
            clauses.append("date(ts) <= :end")
            params["end"] = end.isoformat()
        return self.storage.fetch_sql(
            f"SELECT * FROM orderbook_snapshots_live WHERE {' AND '.join(clauses)} ORDER BY ts",
            params,
        )


def _weather_features_asof(contract: WeatherContract, observations: list[WeatherObservation], timezone_name: str, ts: datetime) -> dict:
    tz = ZoneInfo(timezone_name)
    usable = [obs for obs in observations if obs.temp_f is not None and obs.observed_at <= ts]
    latest = usable[-1] if usable else None
    current = latest.temp_f if latest else None
    max_so_far = max((obs.temp_f for obs in usable if obs.temp_f is not None), default=None)
    min_so_far = min((obs.temp_f for obs in usable if obs.temp_f is not None), default=None)
    temp_1h = _temp_near(usable, ts, hours=1)
    temp_3h = _temp_near(usable, ts, hours=3)
    threshold = contract.threshold
    local_ts = ts.astimezone(tz)
    weather_age = (ts - latest.observed_at).total_seconds() / 60.0 if latest else None
    return {
        "current_temp_asof": current,
        "max_temp_so_far_asof": max_so_far,
        "min_temp_so_far_asof": min_so_far,
        "temp_1h_ago_asof": temp_1h,
        "temp_3h_ago_asof": temp_3h,
        "temp_trend_1h": current - temp_1h if current is not None and temp_1h is not None else None,
        "temp_trend_3h": current - temp_3h if current is not None and temp_3h is not None else None,
        "local_hour": local_ts.hour + local_ts.minute / 60.0,
        "threshold_gap_current": threshold - current if threshold is not None and current is not None else None,
        "threshold_gap_max_so_far": threshold - max_so_far if threshold is not None and max_so_far is not None else None,
        "threshold_gap_min_so_far": min_so_far - threshold if threshold is not None and min_so_far is not None else None,
        "is_threshold_already_hit_asof": _already_hit(contract, max_so_far, min_so_far),
        "observations_count_so_far": len(usable),
        "weather_data_age_minutes": weather_age,
        "forecast_features_available": False,
    }


def _market_features(contract: WeatherContract, candle, ts: datetime) -> dict:
    yes_bid = _num(candle.get("yes_bid"))
    yes_ask = _num(candle.get("yes_ask"))
    yes_mid = (yes_bid + yes_ask) / 2 if yes_bid is not None and yes_ask is not None else None
    close_price = _num(candle.get("close_yes_price"))
    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_mid": yes_mid,
        "no_bid": _num(candle.get("no_bid")),
        "no_ask": _num(candle.get("no_ask")),
        "spread": yes_ask - yes_bid if yes_bid is not None and yes_ask is not None else None,
        "last_trade_price": close_price,
        "volume": _num(candle.get("volume")),
        "open_interest": _num(candle.get("open_interest")),
        "time_to_close_minutes": (contract.close_time - ts).total_seconds() / 60.0 if contract.close_time else None,
        "time_to_expiration_minutes": (contract.expiration_time - ts).total_seconds() / 60.0 if contract.expiration_time else None,
    }


def _already_hit(contract: WeatherContract, max_so_far: float | None, min_so_far: float | None) -> bool:
    if contract.threshold is None:
        return False
    if contract.variable_type == "high_temp" and max_so_far is not None:
        return max_so_far > contract.threshold if contract.comparator == "gt" else max_so_far >= contract.threshold
    if contract.variable_type == "low_temp" and min_so_far is not None:
        return min_so_far < contract.threshold if contract.comparator == "lt" else min_so_far <= contract.threshold
    return False


def _temp_near(observations: list[WeatherObservation], ts: datetime, hours: int) -> float | None:
    target = ts.timestamp() - hours * 3600
    candidates = [obs for obs in observations if obs.temp_f is not None and obs.observed_at.timestamp() <= target]
    if not candidates:
        return None
    return min(candidates, key=lambda obs: abs(obs.observed_at.timestamp() - target)).temp_f


def _parse_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def contracts_by_ticker(contracts: list[WeatherContract]) -> dict[str, WeatherContract]:
    return {contract.market_ticker: contract for contract in contracts}
