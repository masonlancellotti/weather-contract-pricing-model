from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from data.storage import Storage
from data.weather_client import WeatherClient, WeatherObservation
from data.weather_station_mapper import StationMapper
from parsing.weather_contract import WeatherContract


@dataclass(frozen=True)
class RecordedReplayBuildResult:
    markets: int
    snapshots: int
    skipped_markets: int
    warnings: list[str]

    def to_dict(self) -> dict:
        return {"markets": self.markets, "snapshots": self.snapshots, "skipped_markets": self.skipped_markets, "warnings": self.warnings}


class RecordedOrderbookReplayBuilder:
    """Build no-lookahead replay rows from locally recorded full orderbooks."""

    def __init__(self, storage: Storage | None = None, weather_client: WeatherClient | None = None):
        self.storage = storage or Storage()
        self.weather_client = weather_client or WeatherClient()
        self.mapper = StationMapper()
        self._observations_cache: dict[tuple[str, date, str], list[WeatherObservation]] = {}

    def build(
        self,
        start: date | None = None,
        end: date | None = None,
        market_ticker: str | None = None,
        last_days: int | None = None,
        min_settlement_confidence: float = 0.85,
        allow_unsettled: bool = False,
        store_depth_json: bool = False,
    ) -> RecordedReplayBuildResult:
        self.storage.init_db()
        start, end = _date_window(start, end, last_days)
        tickers = self._recorded_tickers(start, end, market_ticker)
        contracts = self._contracts(tickers)
        labels = self._labels(tickers)
        snapshots = 0
        skipped = 0
        warnings: list[str] = []
        for idx, ticker in enumerate(tickers, start=1):
            if idx == 1 or idx % 10 == 0:
                print(f"build-recorded-replay progress markets={idx}/{len(tickers)} snapshots_written={snapshots} skipped={skipped}", flush=True)
            contract = contracts.get(ticker)
            if contract is None:
                skipped += 1
                warnings.append(f"{ticker}: missing parsed weather contract; skipped")
                continue
            label = labels.get(ticker)
            if label is None and not allow_unsettled:
                skipped += 1
                warnings.append(f"{ticker}: missing settlement label; skipped")
                continue
            if label is not None and float(label.get("confidence") or 0.0) < min_settlement_confidence and not allow_unsettled:
                skipped += 1
                warnings.append(f"{ticker}: settlement confidence below {min_settlement_confidence}; skipped")
                continue
            mapping = self.mapper.resolve(contract.city, contract.station_code)
            if mapping is None or contract.local_date is None:
                skipped += 1
                warnings.append(f"{ticker}: missing station mapping/date; skipped")
                continue
            books = self._orderbooks(ticker, start, end)
            if books.empty:
                skipped += 1
                continue
            observations = self._observations(mapping.station_code, contract.local_date, mapping.timezone)
            live_observations = self._live_observations(mapping.station_code, start, end)
            live_forecasts = self._live_forecasts(mapping.station_code, start, end)
            replay_rows: list[dict] = []
            weather_cache: dict[datetime, dict] = {}
            for _, row in books.sort_values("ts").iterrows():
                ts = _parse_dt(row.get("ts"))
                if ts is None:
                    continue
                replay_rows.append(
                    self._build_row(
                        contract,
                        row,
                        label,
                        observations,
                        mapping.timezone,
                        ts,
                        store_depth_json,
                        live_observations,
                        live_forecasts,
                        weather_cache,
                    )
                )
                snapshots += 1
                if len(replay_rows) >= 500:
                    self.storage.upsert_recorded_orderbook_replay_snapshots(replay_rows)
                    replay_rows = []
            self.storage.upsert_recorded_orderbook_replay_snapshots(replay_rows)
            print(f"build-recorded-replay market_done={idx}/{len(tickers)} ticker={ticker} rows={len(books)} snapshots_written={snapshots} skipped={skipped}", flush=True)
        return RecordedReplayBuildResult(markets=len(tickers), snapshots=snapshots, skipped_markets=skipped, warnings=warnings[:100])

    def _recorded_tickers(self, start: date | None, end: date | None, market_ticker: str | None) -> list[str]:
        clauses = ["1=1"]
        params: dict[str, Any] = {}
        if start:
            clauses.append("date(ts) >= :start")
            params["start"] = start.isoformat()
        if end:
            clauses.append("date(ts) <= :end")
            params["end"] = end.isoformat()
        if market_ticker:
            clauses.append("market_ticker = :market_ticker")
            params["market_ticker"] = market_ticker
        frame = self.storage.fetch_sql(f"SELECT market_ticker, COUNT(*) AS snapshots FROM orderbook_snapshots_live WHERE {' AND '.join(clauses)} GROUP BY market_ticker ORDER BY snapshots DESC", params)
        return [] if frame.empty else [str(ticker) for ticker in frame["market_ticker"]]

    def _contracts(self, tickers: list[str]) -> dict[str, WeatherContract]:
        frame = self.storage.fetch_table("parsed_contracts", limit=300000)
        contracts: dict[str, WeatherContract] = {}
        wanted = set(tickers)
        if frame.empty:
            return contracts
        for _, row in frame.sort_values("id", ascending=False).iterrows():
            ticker = str(row.get("market_ticker") or "")
            if ticker not in wanted or ticker in contracts:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            try:
                contract = WeatherContract.model_validate(payload)
            except Exception:
                continue
            if contract.variable_type in {"high_temp", "low_temp"} and contract.local_date is not None:
                contracts[ticker] = contract
        return contracts

    def _labels(self, tickers: list[str]) -> dict[str, dict]:
        frame = self.storage.fetch_table("settlement_labels", limit=300000)
        wanted = set(tickers)
        if frame.empty:
            return {}
        return {str(row["market_ticker"]): row.to_dict() for _, row in frame.iterrows() if str(row.get("market_ticker") or "") in wanted}

    def _orderbooks(self, ticker: str, start: date | None, end: date | None) -> pd.DataFrame:
        clauses = ["market_ticker = :ticker"]
        params: dict[str, Any] = {"ticker": ticker}
        if start:
            clauses.append("date(ts) >= :start")
            params["start"] = start.isoformat()
        if end:
            clauses.append("date(ts) <= :end")
            params["end"] = end.isoformat()
        return self.storage.fetch_sql(f"SELECT * FROM orderbook_snapshots_live WHERE {' AND '.join(clauses)} ORDER BY ts", params)

    def _observations(self, station_code: str, local_date: date, timezone_name: str) -> list[WeatherObservation]:
        key = (station_code.upper(), local_date, timezone_name)
        if key not in self._observations_cache:
            self._observations_cache[key] = self.weather_client.historical_hourly_observations(station_code, local_date, timezone_name)
        return self._observations_cache[key]

    def _live_observations(self, station_code: str, start: date | None, end: date | None) -> pd.DataFrame:
        clauses = ["station_code = :station_code"]
        params: dict[str, Any] = {"station_code": station_code.upper()}
        if start:
            clauses.append("date(ts_recorded) >= :start")
            params["start"] = start.isoformat()
        if end:
            clauses.append("date(ts_recorded) <= :end")
            params["end"] = end.isoformat()
        return self.storage.fetch_sql(f"SELECT * FROM weather_observation_snapshots_live WHERE {' AND '.join(clauses)} ORDER BY ts_recorded", params)

    def _live_forecasts(self, station_code: str, start: date | None, end: date | None) -> pd.DataFrame:
        clauses = ["station_code = :station_code"]
        params: dict[str, Any] = {"station_code": station_code.upper()}
        if start:
            clauses.append("date(ts_recorded) >= :start")
            params["start"] = start.isoformat()
        if end:
            clauses.append("date(ts_recorded) <= :end")
            params["end"] = end.isoformat()
        return self.storage.fetch_sql(f"SELECT * FROM weather_forecast_snapshots_live WHERE {' AND '.join(clauses)} ORDER BY ts_recorded", params)

    def _build_row(
        self,
        contract: WeatherContract,
        book_row,
        label: dict | None,
        observations: list[WeatherObservation],
        timezone_name: str,
        ts: datetime,
        store_depth_json: bool,
        live_observations: pd.DataFrame,
        live_forecasts: pd.DataFrame,
        weather_cache: dict[datetime, dict] | None = None,
    ) -> dict:
        weather_ts = _weather_cache_ts(ts)
        if weather_cache is not None and weather_ts in weather_cache:
            weather = weather_cache[weather_ts]
        else:
            live_weather = recorded_weather_features_asof(contract, live_observations, live_forecasts, timezone_name, weather_ts)
            if live_weather["weather_feature_source"] == "recorded_live_asof":
                weather = live_weather
            else:
                weather = weather_features_asof(contract, observations, timezone_name, weather_ts)
                weather.update(
                    {
                        "weather_feature_source": "historical_reconstructed",
                        "latest_observation_recorded_at": None,
                        "latest_forecast_recorded_at": None,
                        "forecast_high_remaining_f": None,
                        "forecast_low_remaining_f": None,
                        "forecast_max_next_6h_f": None,
                        "forecast_min_next_6h_f": None,
                        "forecast_source": None,
                        "weather_asof_quality_score": max(0.0, min(0.75, 0.5 + 0.05 * weather["observations_count_so_far"])),
                    }
                )
            if weather_cache is not None:
                weather_cache[weather_ts] = weather
        yes_bid = _num(book_row.get("yes_best_bid"))
        yes_ask = _num(book_row.get("yes_best_ask"))
        mid = _num(book_row.get("mid_cents"))
        if mid is None and yes_bid is not None and yes_ask is not None:
            mid = (yes_bid + yes_ask) / 2.0
        row_warnings: list[str] = []
        if yes_bid is None and yes_ask is None:
            row_warnings.append("missing YES bid/ask side")
        if not weather["observations_count_so_far"]:
            row_warnings.append("no weather observations available as-of timestamp")
        if weather["weather_feature_source"] == "historical_reconstructed":
            row_warnings.append("weather features reconstructed after the fact; no recorded live as-of weather snapshot")
        if label is None:
            row_warnings.append("no settlement label; not usable for P&L")
        elif float(label.get("confidence") or 0.0) < 0.85:
            row_warnings.append("settlement label below primary confidence threshold")
        return {
            "market_ticker": contract.market_ticker,
            "event_ticker": contract.event_ticker,
            "ts": ts,
            "city": contract.city,
            "station_code": contract.station_code,
            "local_date": contract.local_date.isoformat() if contract.local_date else None,
            "variable_type": contract.variable_type,
            "contract_type": contract.contract_type,
            "threshold": contract.threshold,
            "comparator": contract.comparator,
            "range_low": contract.range_low,
            "range_high": contract.range_high,
            "unit": contract.unit,
            "yes_best_bid": yes_bid,
            "yes_best_ask": yes_ask,
            "no_best_bid": _num(book_row.get("no_best_bid")),
            "no_best_ask": _num(book_row.get("no_best_ask")),
            "yes_mid": mid,
            "spread_cents": _num(book_row.get("spread_cents")),
            "yes_bids_json": book_row.get("yes_bids_json") if store_depth_json else None,
            "no_bids_json": book_row.get("no_bids_json") if store_depth_json else None,
            "total_yes_bid_depth": _num(book_row.get("total_yes_bid_depth")),
            "total_no_bid_depth": _num(book_row.get("total_no_bid_depth")),
            "depth_yes_bid_1": _num(book_row.get("depth_yes_bid_1")),
            "depth_yes_ask_1": _num(book_row.get("depth_yes_ask_1")),
            "current_temp_asof": weather["current_temp_asof"],
            "max_temp_so_far_asof": weather["max_temp_so_far_asof"],
            "min_temp_so_far_asof": weather["min_temp_so_far_asof"],
            "temp_1h_ago_asof": weather["temp_1h_ago_asof"],
            "temp_3h_ago_asof": weather["temp_3h_ago_asof"],
            "temp_trend_1h": weather["temp_trend_1h"],
            "temp_trend_3h": weather["temp_trend_3h"],
            "local_hour": weather["local_hour"],
            "minutes_to_close": _minutes_until(contract.close_time, ts),
            "minutes_to_settlement": _minutes_until(contract.expiration_time, ts),
            "threshold_gap_current": weather["threshold_gap_current"],
            "threshold_gap_max_so_far": weather["threshold_gap_max_so_far"],
            "threshold_gap_min_so_far": weather["threshold_gap_min_so_far"],
            "is_threshold_already_hit_asof": 1 if weather["is_threshold_already_hit_asof"] else 0,
            "weather_feature_source": weather["weather_feature_source"],
            "latest_observation_recorded_at": weather["latest_observation_recorded_at"],
            "latest_forecast_recorded_at": weather["latest_forecast_recorded_at"],
            "forecast_high_remaining_f": weather["forecast_high_remaining_f"],
            "forecast_low_remaining_f": weather["forecast_low_remaining_f"],
            "forecast_max_next_6h_f": weather["forecast_max_next_6h_f"],
            "forecast_min_next_6h_f": weather["forecast_min_next_6h_f"],
            "forecast_source": weather["forecast_source"],
            "weather_asof_quality_score": weather["weather_asof_quality_score"],
            "settlement_value": _num(label.get("settlement_value")) if label else None,
            "yes_result": int(label["yes_result"]) if label and label.get("yes_result") is not None else None,
            "settlement_confidence": _num(label.get("confidence")) if label else None,
            "settlement_source_type": _settlement_source(label),
            "parser_version": contract.parser_version,
            "settlement_version": str(label.get("settlement_version") or "") if label else None,
            "data_quality_score": _data_quality(contract, label, weather, yes_bid, yes_ask),
            "warnings": "; ".join(row_warnings),
            "raw_json": json.dumps({"source_orderbook_id": int(book_row.get("id") or 0), "weather_observations_count": weather["observations_count_so_far"], "warnings": row_warnings}, default=str),
        }


def weather_features_asof(contract: WeatherContract, observations: list[WeatherObservation], timezone_name: str, ts: datetime) -> dict:
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
    }


def recorded_weather_features_asof(contract: WeatherContract, observations: pd.DataFrame, forecasts: pd.DataFrame, timezone_name: str, ts: datetime) -> dict:
    tz = ZoneInfo(timezone_name)
    if observations.empty:
        return {"weather_feature_source": "missing_recorded_live_weather"}
    obs = observations.copy()
    obs["ts_recorded_dt"] = pd.to_datetime(obs["ts_recorded"], errors="coerce", utc=True)
    obs["ts_observed_dt"] = pd.to_datetime(obs["ts_observed"], errors="coerce", utc=True)
    obs = obs[(obs["ts_recorded_dt"] <= pd.Timestamp(ts)) & (obs["ts_observed_dt"] <= pd.Timestamp(ts))]
    if contract.local_date is not None and not obs.empty:
        obs = obs[obs["ts_observed_dt"].dt.tz_convert(tz).dt.date == contract.local_date]
    obs["temp_f_num"] = pd.to_numeric(obs.get("temp_f", pd.Series(dtype=float)), errors="coerce")
    obs = obs.dropna(subset=["temp_f_num"]).sort_values("ts_observed_dt")
    if obs.empty:
        return {"weather_feature_source": "missing_recorded_live_weather"}
    latest = obs.iloc[-1]
    current = float(latest["temp_f_num"])
    max_so_far = float(obs["temp_f_num"].max())
    min_so_far = float(obs["temp_f_num"].min())
    temp_1h = _recorded_temp_near(obs, ts - timedelta(hours=1))
    temp_3h = _recorded_temp_near(obs, ts - timedelta(hours=3))
    forecast_features = _recorded_forecast_features_asof(contract, forecasts, timezone_name, ts)
    threshold = contract.threshold
    local_ts = ts.astimezone(tz)
    quality = min(float(pd.to_numeric(obs.get("quality_score", pd.Series([0.8])), errors="coerce").fillna(0.8).max()), 1.0)
    if forecast_features["latest_forecast_recorded_at"] is None:
        quality = min(quality, 0.8)
    else:
        quality = min(quality, float(forecast_features["forecast_quality_score"] or 0.8))
    return {
        "weather_feature_source": "recorded_live_asof",
        "current_temp_asof": current,
        "max_temp_so_far_asof": max_so_far,
        "min_temp_so_far_asof": min_so_far,
        "temp_1h_ago_asof": temp_1h,
        "temp_3h_ago_asof": temp_3h,
        "temp_trend_1h": current - temp_1h if temp_1h is not None else None,
        "temp_trend_3h": current - temp_3h if temp_3h is not None else None,
        "local_hour": local_ts.hour + local_ts.minute / 60.0,
        "threshold_gap_current": threshold - current if threshold is not None else None,
        "threshold_gap_max_so_far": threshold - max_so_far if threshold is not None else None,
        "threshold_gap_min_so_far": min_so_far - threshold if threshold is not None else None,
        "is_threshold_already_hit_asof": _already_hit(contract, max_so_far, min_so_far),
        "observations_count_so_far": int(len(obs)),
        "latest_observation_recorded_at": _parse_dt(latest.get("ts_recorded")),
        "weather_asof_quality_score": max(0.0, min(1.0, quality)),
        **forecast_features,
    }


def _recorded_forecast_features_asof(contract: WeatherContract, forecasts: pd.DataFrame, timezone_name: str, ts: datetime) -> dict:
    empty = {
        "latest_forecast_recorded_at": None,
        "forecast_high_remaining_f": None,
        "forecast_low_remaining_f": None,
        "forecast_max_next_6h_f": None,
        "forecast_min_next_6h_f": None,
        "forecast_source": None,
        "forecast_quality_score": None,
    }
    if forecasts.empty:
        return empty
    tz = ZoneInfo(timezone_name)
    frame = forecasts.copy()
    frame["ts_recorded_dt"] = pd.to_datetime(frame["ts_recorded"], errors="coerce", utc=True)
    frame["valid_start_dt"] = pd.to_datetime(frame["forecast_valid_start"], errors="coerce", utc=True)
    frame = frame[(frame["ts_recorded_dt"] <= pd.Timestamp(ts)) & (frame["valid_start_dt"] >= pd.Timestamp(ts))]
    if contract.local_date is not None and not frame.empty:
        frame = frame[frame["valid_start_dt"].dt.tz_convert(tz).dt.date == contract.local_date]
    if frame.empty:
        return empty
    latest_recorded = frame["ts_recorded_dt"].max()
    frame = frame[frame["ts_recorded_dt"] == latest_recorded].copy()
    frame["temp_f_num"] = pd.to_numeric(frame.get("temp_f", pd.Series(dtype=float)), errors="coerce")
    temps = frame["temp_f_num"].dropna()
    next_6h = frame[frame["valid_start_dt"] <= pd.Timestamp(ts + timedelta(hours=6))]["temp_f_num"].dropna()
    return {
        "latest_forecast_recorded_at": latest_recorded.to_pydatetime(),
        "forecast_high_remaining_f": float(temps.max()) if not temps.empty else None,
        "forecast_low_remaining_f": float(temps.min()) if not temps.empty else None,
        "forecast_max_next_6h_f": float(next_6h.max()) if not next_6h.empty else None,
        "forecast_min_next_6h_f": float(next_6h.min()) if not next_6h.empty else None,
        "forecast_source": str(frame["source"].dropna().iloc[0]) if "source" in frame and frame["source"].notna().any() else None,
        "forecast_quality_score": float(pd.to_numeric(frame.get("quality_score", pd.Series([0.8])), errors="coerce").fillna(0.8).max()),
    }


def _recorded_temp_near(observations: pd.DataFrame, target: datetime) -> float | None:
    candidates = observations[observations["ts_observed_dt"] <= pd.Timestamp(target)]
    if candidates.empty:
        return None
    deltas = (candidates["ts_observed_dt"] - pd.Timestamp(target)).abs()
    return float(candidates.loc[deltas.idxmin(), "temp_f_num"])


def _already_hit(contract: WeatherContract, max_so_far: float | None, min_so_far: float | None) -> bool:
    if contract.contract_type == "range_bucket":
        if contract.variable_type == "high_temp" and max_so_far is not None and contract.range_high is not None:
            return max_so_far > contract.range_high
        if contract.variable_type == "low_temp" and min_so_far is not None and contract.range_low is not None:
            return min_so_far < contract.range_low
        return False
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


def _data_quality(contract: WeatherContract, label: dict | None, weather: dict, yes_bid: float | None, yes_ask: float | None) -> float:
    score = min(1.0, float(contract.parse_confidence or 0.0))
    if label is None:
        score -= 0.35
    else:
        score = min(score, float(label.get("confidence") or 0.0))
    if not weather["observations_count_so_far"]:
        score -= 0.25
    score = min(score, float(weather.get("weather_asof_quality_score") or score))
    if yes_bid is None and yes_ask is None:
        score -= 0.15
    return max(0.0, min(1.0, score))


def _date_window(start: date | None, end: date | None, last_days: int | None) -> tuple[date | None, date | None]:
    if last_days is None:
        return start, end
    end_date = end or date.today()
    return end_date - timedelta(days=max(last_days, 1)), end_date


def _settlement_source(label: dict | None) -> str | None:
    if not label:
        return None
    return str(label.get("exact_source_type") or label.get("source") or label.get("fallback_source_type") or "unknown")


def _minutes_until(target: datetime | None, ts: datetime) -> float | None:
    if target is None:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return (target - ts).total_seconds() / 60.0


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _weather_cache_ts(ts: datetime) -> datetime:
    minute = (ts.minute // 5) * 5
    return ts.replace(minute=minute, second=0, microsecond=0)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
