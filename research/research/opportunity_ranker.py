from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from backtest.execution import NormalizedOrderBook
from config import PROJECT_ROOT, settings
from data.kalshi_client import KalshiClient
from data.kalshi_market_loader import KalshiMarketLoader
from data.storage import Storage
from data.weather_station_mapper import StationMapper
from models.weather_fair_value import WeatherFairValueModel
from parsing.market_parser import WeatherMarketParser
from research.edge_types import FAIR_VALUE_TAKER_EDGE, PASSIVE_LIQUIDITY_SPREAD_EDGE, confidence_level
from risk.risk_engine import RiskEngine


@dataclass(frozen=True)
class OpportunityRankResult:
    rows: list[dict[str, Any]]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"rows": self.rows[:100], "warnings": self.warnings, "message": "Ranking is for research/paper testing, not live trading."}

    def to_text(self) -> str:
        lines = ["Ranking is for research/paper testing, not live trading."]
        for row in self.rows[:25]:
            lines.append(
                f"{row['recommended_action']} {row['market_ticker']} edge_after_buffers={row['edge_after_buffers_cents']:.2f} "
                f"fair={row['fair_yes_price']:.2f} bid/ask={row['yes_bid']}/{row['yes_ask']} reason={row['reason']}"
            )
        if self.warnings:
            lines.append("warnings:")
            lines.extend(f"- {warning}" for warning in self.warnings[:50])
        return "\n".join(lines)


class OpportunityRanker:
    def __init__(self, storage: Storage | None = None, client: KalshiClient | None = None):
        self.storage = storage or Storage()
        self.client = client or KalshiClient()
        self.parser = WeatherMarketParser()
        self.mapper = StationMapper()
        self.model = WeatherFairValueModel()
        self.risk = RiskEngine()
        self.loader = KalshiMarketLoader(client=self.client, storage=self.storage, parser=self.parser)

    def rank(self, weather_only: bool = True, max_markets: int = 100, persist_exports: bool = True) -> OpportunityRankResult:
        markets = self.loader.load_active_weather_markets(persist=True, max_pages=1, max_series=max(1, min(max_markets, 25))) if weather_only else list(self.client.iter_markets(status="open", limit=1000, max_pages=1))
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        for market in markets[:max_markets]:
            try:
                row = self._rank_market(market)
                rows.append(row)
            except Exception as exc:
                warnings.append(f"{market.get('ticker')}: ranking failed: {exc}")
        rows = sorted(rows, key=lambda item: item.get("edge_after_buffers_cents", -999), reverse=True)
        if persist_exports:
            reports = PROJECT_ROOT / "reports"
            reports.mkdir(exist_ok=True)
            pd.DataFrame(rows).to_csv(reports / "fair_value_candidates.csv", index=False)
            pd.DataFrame([row for row in rows if row["recommended_action"] == "SKIP"]).to_csv(reports / "skipped_due_to_data_quality.csv", index=False)
        return OpportunityRankResult(rows, warnings)

    def _rank_market(self, market: dict[str, Any]) -> dict[str, Any]:
        contract = self.parser.parse(market)
        mapping = self.mapper.resolve(contract.city, contract.station_code)
        raw = self.client.get_orderbook(contract.market_ticker, depth=100)
        book = NormalizedOrderBook.from_kalshi(contract.market_ticker, raw)
        features = self._current_weather_features(mapping.station_code if mapping else None)
        fair = self.model.estimate(contract, features)
        yes_bid = book.yes_bid
        yes_ask = book.yes_ask
        no_ask = book.no_ask
        buy_yes_edge = fair.fair_yes_price_cents - yes_ask if yes_ask is not None else None
        buy_no_edge = fair.fair_no_price_cents - no_ask if no_ask is not None else None
        best_edge = max([value for value in [buy_yes_edge, buy_no_edge] if value is not None], default=-999.0)
        action = "SKIP"
        edge_type = FAIR_VALUE_TAKER_EDGE
        reason = fair.no_trade_reason or fair.explanation
        if contract.settlement_source is None:
            reason = "Settlement source unclear."
        elif mapping is None or mapping.confidence < 0.75:
            reason = "Station mapping missing or low confidence."
        elif fair.no_trade_reason:
            reason = fair.no_trade_reason
        elif best_edge - fair.uncertainty_cents - 1.0 >= settings.min_edge_after_buffers_cents:
            action = "TAKER_BUY_YES_CANDIDATE" if (buy_yes_edge or -999) >= (buy_no_edge or -999) else "TAKER_BUY_NO_CANDIDATE"
        elif book.spread is not None and book.spread >= settings.passive_min_spread_cents and fair.confidence >= 0.55:
            action = "PASSIVE_QUOTE_CANDIDATE"
            edge_type = PASSIVE_LIQUIDITY_SPREAD_EDGE
            reason = "Wide spread around fair value; passive research candidate only."
        elif best_edge > 0:
            action = "WATCH"
        risk_payload = {
            "market_ticker": contract.market_ticker,
            "contract_type": contract.contract_type,
            "settlement_quality_score": mapping.confidence if mapping else 0.0,
            "weather_data_age_minutes": features.get("weather_data_age_minutes", 999),
            "forecast_data_age_minutes": features.get("forecast_data_age_minutes", 999),
            "edge_after_buffers_cents": best_edge - fair.uncertainty_cents - 1.0,
            "depth": min(book.depth_at_best_bid, book.depth_at_best_ask),
            "intended_price": yes_ask or no_ask or 100,
        }
        risk = self.risk.evaluate_candidate(risk_payload)
        if action not in {"SKIP", "WATCH"} and not risk.approved:
            action = "SKIP"
            reason = risk.reason
        return {
            "market_ticker": contract.market_ticker,
            "title": contract.title,
            "contract_type": contract.contract_type,
            "station": mapping.station_code if mapping else None,
            "settlement_source": contract.settlement_source,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": book.no_bid,
            "no_ask": no_ask,
            "spread": book.spread,
            "depth": min(book.depth_at_best_bid, book.depth_at_best_ask),
            "fair_yes_price": fair.fair_yes_price_cents,
            "fair_no_price": fair.fair_no_price_cents,
            "uncertainty_cents": fair.uncertainty_cents,
            "executable_buy_yes_edge": buy_yes_edge,
            "executable_buy_no_edge": buy_no_edge,
            "edge_after_uncertainty": best_edge - fair.uncertainty_cents,
            "edge_after_fees": best_edge - 1.0,
            "edge_after_buffers_cents": best_edge - fair.uncertainty_cents - 1.0,
            "recommended_action": action,
            "edge_type": edge_type,
            "confidence_level": confidence_level(fair.confidence),
            "data_quality_score": features.get("weather_asof_quality_score", features.get("data_quality_score", 0.0)),
            "settlement_quality_score": mapping.confidence if mapping else 0.0,
            "parser_version": contract.parser_version,
            "reason": reason,
            "raw_json": json.dumps({"fair": fair.to_dict(), "features": features, "risk": risk.to_dict()}, default=str),
        }

    def _current_weather_features(self, station_code: str | None) -> dict[str, Any]:
        if not station_code:
            return {"weather_asof_quality_score": 0.0, "weather_data_age_minutes": 999, "forecast_data_age_minutes": 999}
        obs = self.storage.fetch_sql(
            "SELECT * FROM weather_observation_snapshots_live WHERE station_code = :station ORDER BY ts_recorded DESC LIMIT 20",
            {"station": station_code.upper()},
        )
        fcst = self.storage.fetch_sql(
            "SELECT * FROM weather_forecast_snapshots_live WHERE station_code = :station ORDER BY ts_recorded DESC LIMIT 200",
            {"station": station_code.upper()},
        )
        now = datetime.now(timezone.utc)
        features: dict[str, Any] = {"weather_asof_quality_score": 0.0, "weather_data_age_minutes": 999, "forecast_data_age_minutes": 999}
        if not obs.empty:
            latest = obs.iloc[0]
            ts_recorded = _parse_ts(latest.get("ts_recorded"))
            temps = pd.to_numeric(obs.get("temp_f", pd.Series(dtype=float)), errors="coerce").dropna()
            features.update(
                {
                    "current_temp_asof": _num(latest.get("temp_f")),
                    "max_temp_so_far_asof": float(temps.max()) if not temps.empty else None,
                    "min_temp_so_far_asof": float(temps.min()) if not temps.empty else None,
                    "temp_trend_1h": None,
                    "weather_asof_quality_score": _num(latest.get("quality_score")) or 0.8,
                    "weather_data_age_minutes": (now - ts_recorded).total_seconds() / 60.0 if ts_recorded else 999,
                }
            )
        if not fcst.empty:
            latest_recorded = _parse_ts(fcst.iloc[0].get("ts_recorded"))
            same_snapshot = fcst[fcst["ts_recorded"].astype(str) == str(fcst.iloc[0].get("ts_recorded"))]
            future = same_snapshot[pd.to_datetime(same_snapshot["forecast_valid_start"], errors="coerce", utc=True) >= pd.Timestamp(now)]
            temps = pd.to_numeric(future.get("temp_f", pd.Series(dtype=float)), errors="coerce").dropna()
            features.update(
                {
                    "forecast_high_remaining_f": float(temps.max()) if not temps.empty else None,
                    "forecast_low_remaining_f": float(temps.min()) if not temps.empty else None,
                    "forecast_data_age_minutes": (now - latest_recorded).total_seconds() / 60.0 if latest_recorded else 999,
                }
            )
        return features


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
