"""
tests/test_model.py — Tests for weather model and pricing logic.
Run: pytest tests/test_model.py -v
"""
from decimal import Decimal
from datetime import datetime, timezone

from weather.model import build_model, prob_in_range, _normal_cdf, snapshots_to_buckets
from strategy.pricing import compute_edge
from strategy.sizing import size_signal
from models import MarketSnapshot, ModelOutput, Venue
from storage import DB
from utils.time import can_open_new_same_day_positions, city_local_now, hours_until_entry_cutoff


# ── Model tests ───────────────────────────────────────────────────────────────

def test_normal_cdf_midpoint():
    """CDF at the mean should be 0.5."""
    assert abs(_normal_cdf(70, 70, 5) - 0.5) < 1e-6


def test_prob_in_range_full():
    """Unbounded range should give probability ~1.0."""
    p = prob_in_range(None, None, 70, 5)
    assert p == 1.0


def test_prob_in_range_symmetric():
    """P(mu-sigma < X < mu+sigma) ≈ 68.27%."""
    mu, sigma = 72.0, 4.0
    p = prob_in_range(mu - sigma, mu + sigma, mu, sigma)
    assert abs(p - 0.6827) < 0.01


def test_bucket_probs_sum_to_one():
    """All bucket probabilities should sum to ~1."""
    from weather.mapping import CITY_META
    from weather.model import _compute_bucket_probs

    buckets = [
        {"label": "<60", "min": None, "max": 60},
        {"label": "60-65", "min": 60, "max": 65},
        {"label": "65-70", "min": 65, "max": 70},
        {"label": "70-75", "min": 70, "max": 75},
        {"label": ">=75", "min": 75, "max": None},
    ]
    probs = _compute_bucket_probs(buckets, mu=68.0, sigma=4.0)
    total = sum(probs.values())
    assert abs(total - 1.0) < 0.02


def test_build_model_no_data():
    """With no historical data, model should return low confidence."""
    from weather.mapping import CITY_META
    city = CITY_META["NYC"]
    buckets = [
        {"label": "<70", "min": None, "max": 70},
        {"label": ">=70", "min": 70, "max": None},
    ]
    result = build_model(city, forecast=None, obs=None, historical_highs=[], contract_buckets=buckets)
    assert result.confidence == 0.0


def test_build_model_with_good_data():
    """With adequate data, model should produce meaningful output."""
    from weather.mapping import CITY_META
    from models import WeatherForecast
    city = CITY_META["NYC"]

    # Simulate 30 days of history around 70°F
    history = [{"date": f"2024-03-{i+1:02d}", "high_f": 68.0 + i * 0.1} for i in range(30)]
    forecast = WeatherForecast(
        station_id="KNYC", city="NYC", forecast_for="2024-04-01",
        high_f=71.5, low_f=55.0, hourly=[], source="nws_gridpoint"
    )
    buckets = [
        {"label": "<68", "min": None, "max": 68},
        {"label": "68-72", "min": 68, "max": 72},
        {"label": "72-76", "min": 72, "max": 76},
        {"label": ">=76", "min": 76, "max": None},
    ]
    result = build_model(city, forecast=forecast, obs=None, historical_highs=history, contract_buckets=buckets)

    assert result.confidence > 0.5
    assert abs(result.expected_high - 70.8) < 2.0  # blend of 71.5 and ~69.5
    assert abs(sum(result.bucket_probs.values()) - 1.0) < 0.02


def test_build_model_same_day_uses_observed_high_anchor():
    from weather.mapping import CITY_META
    from models import WeatherForecast

    city = CITY_META["NYC"]
    history = [{"date": f"2024-03-{i+1:02d}", "high_f": 68.0 + i * 0.1} for i in range(30)]
    forecast = WeatherForecast(
        station_id="KNYC",
        city="NYC",
        forecast_for="2024-04-01",
        high_f=60.0,
        low_f=48.0,
        hourly=[],
        source="nws_gridpoint"
    )
    result = build_model(
        city,
        forecast=forecast,
        obs=None,
        historical_highs=history,
        contract_buckets=[
            {"label": "<68", "min": None, "max": 68},
            {"label": "68-72", "min": 68, "max": 72},
        ],
        observed_high_so_far=71.0,
        current_local_date="2024-04-01",
    )
    assert result.expected_high >= 67.0


def test_build_model_missing_intraday_obs_reduces_confidence():
    from weather.mapping import CITY_META
    from models import WeatherForecast

    city = CITY_META["NYC"]
    history = [{"date": f"2024-03-{i+1:02d}", "high_f": 68.0 + i * 0.1} for i in range(30)]
    forecast = WeatherForecast(
        station_id="KNYC",
        city="NYC",
        forecast_for="2024-04-01",
        high_f=62.0,
        low_f=50.0,
        hourly=[],
        source="nws_gridpoint"
    )
    with_high = build_model(
        city,
        forecast=forecast,
        obs=None,
        historical_highs=history,
        contract_buckets=[{"label": "<68", "min": None, "max": 68}],
        observed_high_so_far=61.0,
        current_local_date="2024-04-01",
    )
    without_high = build_model(
        city,
        forecast=forecast,
        obs=None,
        historical_highs=history,
        contract_buckets=[{"label": "<68", "min": None, "max": 68}],
        observed_high_so_far=None,
        current_local_date="2024-04-01",
    )
    assert without_high.confidence < with_high.confidence


# ── Pricing tests ─────────────────────────────────────────────────────────────

def _make_snapshot(
    best_bid: float,
    best_ask: float,
    city: str = "NYC",
    settlement_date: str = "2024-04-01",
    bucket_label: str = "68-72",
    bucket_min: float = 68.0,
    bucket_max: float = 72.0,
) -> MarketSnapshot:
    return MarketSnapshot(
        venue=Venue.KALSHI,
        market_id="KXHIGHNY-24APR01-T70",
        city=city,
        bucket_label=bucket_label,
        bucket_min=bucket_min,
        bucket_max=bucket_max,
        settlement_date=settlement_date,
        best_bid=Decimal(str(best_bid)),
        best_ask=Decimal(str(best_ask)),
        last_price=Decimal(str((best_bid + best_ask) / 2)),
        yes_bid=None,
        yes_ask=None,
    )


def _make_model(fair_prob: float, confidence: float = 0.9) -> ModelOutput:
    return ModelOutput(
        city="NYC",
        settlement_date="2024-04-01",
        expected_high=70.0,
        std_dev=4.0,
        bucket_probs={"68-72": fair_prob, "<68": (1 - fair_prob) / 2, ">=72": (1 - fair_prob) / 2},
        confidence=confidence,
        obs_temp=69.0,
        forecast_temp=70.5,
    )


def test_no_signal_when_market_is_fair():
    """Should return None when market price matches fair value."""
    snap = _make_snapshot(0.48, 0.52)
    model = _make_model(0.50)  # fair prob = 0.50 * 0.9 + 0.5 * 0.1 = 0.50
    signal = compute_edge(snap, model)
    assert signal is None


def test_signal_when_market_underpriced():
    """Should return YES signal when market is well below fair value."""
    snap = _make_snapshot(0.30, 0.34)  # market mid = 0.32
    model = _make_model(0.60, confidence=1.0)  # fair = 0.60
    signal = compute_edge(snap, model)
    assert signal is not None
    assert signal.side.value == "yes"
    assert signal.edge > 0


def test_no_signal_when_edge_below_threshold():
    """Edge just below fee+threshold should return None."""
    snap = _make_snapshot(0.44, 0.48)  # market mid = 0.46
    model = _make_model(0.50, confidence=1.0)  # fair = 0.50, edge = 0.04
    # MIN_EDGE_THRESHOLD=0.06 + fees = 0.11 — not enough
    signal = compute_edge(snap, model)
    assert signal is None


def test_price_sanity_bounds():
    """Suggested price should always be in [0.01, 0.99]."""
    snap = _make_snapshot(0.01, 0.02)
    model = _make_model(0.95, confidence=1.0)
    signal = compute_edge(snap, model)
    if signal:
        assert 0.01 <= float(signal.suggested_price) <= 0.99


def test_no_side_suggested_price_uses_no_probability_space():
    today = city_local_now("NYC").date().isoformat()
    snap = _make_snapshot(
        0.98,
        0.99,
        city="NYC",
        settlement_date=today,
        bucket_label="68-72",
        bucket_min=68.0,
        bucket_max=72.0,
    )
    model = ModelOutput(
        city="NYC",
        settlement_date=today,
        expected_high=60.0,
        std_dev=1.0,
        bucket_probs={"68-72": 0.01},
        confidence=1.0,
        obs_temp=60.0,
        forecast_temp=60.0,
        observed_high_so_far=60.0,
        remaining_forecast_high=60.0,
        local_hour=12,
    )
    signal = compute_edge(snap, model)
    assert signal is not None
    assert signal.side.value == "no"
    assert float(signal.suggested_price) <= 0.03


def test_size_signal_scales_with_edge(tmp_path):
    db = DB(str(tmp_path / "sizing.db"))
    db.connect()
    snap = _make_snapshot(0.30, 0.34)
    weak = _make_model(0.48, confidence=1.0)
    strong = _make_model(0.70, confidence=1.0)
    weak_signal = compute_edge(snap, weak)
    strong_signal = compute_edge(snap, strong)
    assert strong_signal is not None
    sized_strong = size_signal(strong_signal, snap, strong, db)
    assert sized_strong is not None
    if weak_signal is not None:
        sized_weak = size_signal(weak_signal, snap, weak, db)
        if sized_weak is not None:
            assert sized_strong.suggested_contracts >= sized_weak.suggested_contracts
    db.close()


# ── Risk tests ────────────────────────────────────────────────────────────────

def test_pre_flight_live_disabled(tmp_path):
    """pre_flight_check should reject when LIVE_ENABLED=false."""
    from storage import DB
    from strategy.risk import pre_flight_check
    from strategy.pricing import TradeSignal
    from models import Side
    from unittest.mock import patch

    test_db = DB(str(tmp_path / "test.db"))
    test_db.connect()

    signal = TradeSignal(
        venue=Venue.KALSHI, market_id="TEST", city="NYC",
        bucket_label="68-72", side=Side.YES,
        fair_prob=0.65, market_prob=0.40, edge=0.25,
        suggested_price=Decimal("0.43"), suggested_contracts=2,
        reason="test",
    )
    with patch("strategy.risk.check_live_enabled", return_value=False):
        ok, reason = pre_flight_check(signal, Decimal("100"), test_db)
    assert not ok
    assert "live_not_enabled" in reason
    test_db.close()


def test_pre_flight_respects_balance_fraction_cap(tmp_path, monkeypatch):
    from strategy.risk import pre_flight_check
    from strategy.pricing import TradeSignal
    from models import Side

    monkeypatch.setattr("strategy.risk.check_kill_switch", lambda: False)
    monkeypatch.setattr("strategy.risk.check_live_enabled", lambda: True)

    test_db = DB(str(tmp_path / "balance_cap.db"))
    test_db.connect()
    signal = TradeSignal(
        venue=Venue.KALSHI,
        market_id="TEST",
        city="NYC",
        bucket_label="68-72",
        side=Side.YES,
        fair_prob=0.65,
        market_prob=0.40,
        edge=0.25,
        suggested_price=Decimal("0.60"),
        suggested_contracts=5,
        reason="test",
    )
    ok, reason = pre_flight_check(signal, Decimal("10"), test_db)
    assert not ok
    assert "bankroll_cap" in reason
    test_db.close()


def test_time_gate_blocks_after_cutoff():
    ny_after = datetime(2024, 4, 1, 20, 0, tzinfo=timezone.utc)  # 4 PM EDT
    chi_after = datetime(2024, 4, 1, 21, 0, tzinfo=timezone.utc)  # 4 PM CDT
    la_after = datetime(2024, 4, 1, 23, 0, tzinfo=timezone.utc)  # 4 PM PDT
    assert not can_open_new_same_day_positions("NYC", ny_after)
    assert not can_open_new_same_day_positions("CHI", chi_after)
    assert not can_open_new_same_day_positions("LA", la_after)
    assert hours_until_entry_cutoff("NYC", ny_after) == 0.0


def test_time_gate_blocks_before_start():
    ny_before = datetime(2024, 4, 1, 9, 30, tzinfo=timezone.utc)  # 5:30 AM EDT
    chi_before = datetime(2024, 4, 1, 10, 30, tzinfo=timezone.utc)  # 5:30 AM CDT
    la_before = datetime(2024, 4, 1, 12, 30, tzinfo=timezone.utc)  # 5:30 AM PDT
    assert not can_open_new_same_day_positions("NYC", ny_before)
    assert not can_open_new_same_day_positions("CHI", chi_before)
    assert not can_open_new_same_day_positions("LA", la_before)


def test_exposure_includes_positions(tmp_path):
    from models import Position, Side

    db = DB(str(tmp_path / "exposure.db"))
    db.connect()
    db.upsert_position(
        Position(
            venue=Venue.KALSHI,
            market_id="KXHIGHNY-24APR01-T70",
            city="NYC",
            bucket_label="68-72",
            side=Side.YES,
            contracts=4,
            avg_price=Decimal("0.40"),
        )
    )
    assert db.get_total_exposure(Venue.KALSHI) == Decimal("1.60")
    assert db.get_city_exposure(Venue.KALSHI, "NYC") == Decimal("1.60")
    db.close()


def test_late_day_market_certainty_blocks_no_fade():
    today = city_local_now("LA").date().isoformat()
    snap = _make_snapshot(
        0.98,
        0.99,
        city="LA",
        settlement_date=today,
        bucket_label="66 to 67",
        bucket_min=66.0,
        bucket_max=67.0,
    )
    model = ModelOutput(
        city="LA",
        settlement_date=today,
        expected_high=66.2,
        std_dev=2.0,
        bucket_probs={"66 to 67": 0.22},
        confidence=1.0,
        obs_temp=66.0,
        forecast_temp=66.4,
        observed_high_so_far=66.0,
        remaining_forecast_high=66.4,
        local_hour=22,
    )
    assert compute_edge(snap, model) is None


def test_bucket_locked_in_blocks_no_signal():
    today = city_local_now("LA").date().isoformat()
    snap = _make_snapshot(
        0.98,
        0.99,
        city="LA",
        settlement_date=today,
        bucket_label="66 to 67",
        bucket_min=66.0,
        bucket_max=67.0,
    )
    model = ModelOutput(
        city="LA",
        settlement_date=today,
        expected_high=66.3,
        std_dev=1.0,
        bucket_probs={"66 to 67": 0.15},
        confidence=1.0,
        obs_temp=66.2,
        forecast_temp=66.4,
        observed_high_so_far=66.2,
        remaining_forecast_high=66.4,
        local_hour=14,
    )
    assert compute_edge(snap, model) is None


def test_bucket_already_exceeded_blocks_yes_signal():
    today = city_local_now("LA").date().isoformat()
    snap = _make_snapshot(
        0.08,
        0.10,
        city="LA",
        settlement_date=today,
        bucket_label="66 to 67",
        bucket_min=66.0,
        bucket_max=67.0,
    )
    model = ModelOutput(
        city="LA",
        settlement_date=today,
        expected_high=68.2,
        std_dev=1.0,
        bucket_probs={"66 to 67": 0.95},
        confidence=1.0,
        obs_temp=68.1,
        forecast_temp=68.4,
        observed_high_so_far=68.1,
        remaining_forecast_high=68.4,
        local_hour=13,
    )
    assert compute_edge(snap, model) is None
