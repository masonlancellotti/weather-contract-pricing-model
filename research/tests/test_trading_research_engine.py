from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd

from backtest.execution import NormalizedOrderBook
from backtest.passive_fill_model import PassiveFillConfig, PassiveFillType, PassiveQuote, adverse_selection_cents, simulate_passive_fill
from config import settings
from data.storage import Storage
from live.paper_trader import PaperTrader
from models.weather_fair_value import WeatherFairValueModel
from parsing.weather_contract import WeatherContract
from research.signal_validation import future_price_validation_for_signal
from research.liquidity_analysis import _future_mid_after
from research.trading_readiness import TradingReadiness
from risk.risk_engine import RiskDecision, RiskEngine


def _storage(tmp_path) -> Storage:
    return Storage(replace(settings, database_url=f"sqlite:///{tmp_path / 'test.db'}"))


def test_no_midpoint_fill_orderbook_math_uses_ask():
    raw = {"orderbook_fp": {"yes_dollars": [["0.42", "10"]], "no_dollars": [["0.53", "7"]]}}
    book = NormalizedOrderBook.from_kalshi("T", raw)
    assert book.mid == 44.5
    assert book.yes_ask == 47
    assert book.yes_ask != book.mid


def test_passive_touched_quote_does_not_fill():
    quote = PassiveQuote("M", "BUY_YES", 45, 1)
    future = pd.DataFrame([{"ts": datetime(2026, 5, 1, 12, tzinfo=timezone.utc), "yes_best_ask": 45, "depth_yes_bid_1": 10}])
    result = simulate_passive_fill(quote, future, PassiveFillConfig(assume_touch_fill=False, require_traded_through=True))
    assert result.fill_type == PassiveFillType.TOUCHED_ONLY_NO_FILL
    assert not result.filled


def test_passive_traded_through_quote_fills_with_haircut_and_penalty():
    quote = PassiveQuote("M", "BUY_YES", 45, 4)
    future = pd.DataFrame([{"ts": datetime(2026, 5, 1, 12, tzinfo=timezone.utc), "yes_best_ask": 43, "depth_yes_bid_1": 20}])
    result = simulate_passive_fill(quote, future, PassiveFillConfig(fill_haircut=0.25, adverse_selection_penalty_cents=2))
    assert result.fill_type == PassiveFillType.PARTIAL_FILL_CONSERVATIVE
    assert result.fill_quantity == 1
    assert result.fill_price == 47


def test_adverse_selection_metric_directional():
    assert adverse_selection_cents("BUY_YES", 45, 40) == -5
    assert adverse_selection_cents("BUY_NO", 45, 60) == -5


def test_fair_value_threshold_and_range_bucket_probabilities():
    model = WeatherFairValueModel()
    threshold = WeatherContract(event_ticker="E", market_ticker="T", variable_type="high_temp", contract_type="threshold_above", threshold=70, comparator="gte", parse_confidence=0.9, station_confidence=0.9)
    features = {"current_temp_asof": 68, "max_temp_so_far_asof": 69, "forecast_high_remaining_f": 75, "local_hour": 12, "weather_asof_quality_score": 0.9}
    assert model.estimate(threshold, features).fair_yes_probability > 0.7
    bucket = WeatherContract(event_ticker="E", market_ticker="B", variable_type="high_temp", contract_type="range_bucket", range_low=70, range_high=72, parse_confidence=0.9, station_confidence=0.9)
    bucket_prob = model.estimate(bucket, features).fair_yes_probability
    assert 0.05 < bucket_prob < 0.6


def test_risk_engine_rejects_stale_and_low_confidence_and_small_edge():
    risk = RiskEngine()
    base = {"contract_type": "threshold_above", "settlement_quality_score": 0.9, "weather_data_age_minutes": 1, "forecast_data_age_minutes": 1, "edge_after_buffers_cents": 8, "depth": 10, "market_ticker": "M", "intended_price": 50}
    assert risk.evaluate_candidate(base).decision == RiskDecision.APPROVE
    assert risk.evaluate_candidate({**base, "weather_data_age_minutes": 99}).decision == RiskDecision.REJECT_DATA_STALE
    assert risk.evaluate_candidate({**base, "settlement_quality_score": 0.5}).decision == RiskDecision.REJECT_LOW_SETTLEMENT_CONFIDENCE
    assert risk.evaluate_candidate({**base, "edge_after_buffers_cents": 1}).decision == RiskDecision.REJECT_EDGE_TOO_SMALL


def test_future_price_validation_for_buy_yes_and_buy_no():
    replay = pd.DataFrame(
        [
            {"ts": datetime(2026, 5, 1, 12, 5, tzinfo=timezone.utc), "yes_mid": 55},
            {"ts": datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc), "yes_mid": 60},
        ]
    )
    yes = future_price_validation_for_signal({"ts": datetime(2026, 5, 1, 12, tzinfo=timezone.utc), "action": "BUY_YES", "entry_price": 50}, replay)
    no = future_price_validation_for_signal({"ts": datetime(2026, 5, 1, 12, tzinfo=timezone.utc), "action": "BUY_NO", "entry_price": 50}, replay)
    assert yes["beat_5m"] is True
    assert no["beat_5m"] is False


def test_future_price_validation_handles_naive_replay_timestamps():
    replay = pd.DataFrame(
        [
            {"ts": "2026-05-01 12:05:00", "yes_mid": 55},
            {"ts": "2026-05-01 12:30:00", "yes_mid": 60},
        ]
    )
    result = future_price_validation_for_signal({"ts": "2026-05-01 12:00:00", "action": "BUY_YES", "entry_price": 50}, replay)
    assert result["beat_5m"] is True


def test_liquidity_future_mid_handles_mixed_timezone_timestamps():
    future = pd.DataFrame(
        [
            {"ts": "2026-05-01 12:20:00", "yes_mid": 52},
            {"ts": datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc), "yes_mid": 56},
        ]
    )
    assert _future_mid_after(future, datetime(2026, 5, 1, 12, 0), 30) == 56


def test_paper_trading_only_logs_orders(monkeypatch, tmp_path):
    class FakeRanker:
        def __init__(self, storage=None):
            pass

        def rank(self, weather_only=True, max_markets=100, persist_exports=True):
            class Result:
                rows = [
                    {
                        "market_ticker": "M",
                        "recommended_action": "TAKER_BUY_YES_CANDIDATE",
                        "edge_type": "FAIR_VALUE_TAKER_EDGE",
                        "yes_ask": 40,
                        "no_ask": 60,
                        "fair_yes_price": 55,
                        "edge_after_buffers_cents": 8,
                        "reason": "test",
                        "raw_json": "{}",
                    }
                ]

            return Result()

    monkeypatch.setattr("live.paper_trader.OpportunityRanker", FakeRanker)
    storage = _storage(tmp_path)
    result = PaperTrader(storage=storage).run_once()
    assert result["orders_logged"] == 1
    assert len(storage.fetch_table("paper_orders")) == 1


def test_stale_runs_excluded_from_readiness(tmp_path):
    storage = _storage(tmp_path)
    storage.insert_recorded_strategy_sweep(
        {
            "ts": datetime(2026, 5, 1, tzinfo=timezone.utc),
            "strategy": "already_hit",
            "mode": "taker",
            "params_json": "{}",
            "label_quality": "primary",
            "markets": 10,
            "snapshots": 1000,
            "signals": 100,
            "fills": 100,
            "gross_pnl": 1000,
            "fees": 100,
            "net_pnl": 900,
            "roi": 0.1,
            "win_rate": 0.8,
            "max_drawdown": -50,
            "robustness_verdict": "passes",
            "recommendation": "PAPER_READY_SPECIFIC_STRATEGY",
            "parser_version": "old",
            "settlement_version": "old",
            "strategy_version": "old",
            "parameter_hash": "old",
            "is_stale": 1,
            "raw_json": "{}",
        }
    )
    result = TradingReadiness(storage).evaluate(last_days=30)
    assert result.status != "PAPER_READY_SPECIFIC_STRATEGY"
