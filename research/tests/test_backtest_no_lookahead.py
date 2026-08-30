from datetime import datetime, timezone

from backtest.runner import BacktestRunner


def test_backtest_refuses_to_claim_edge_without_replay_data():
    result = BacktestRunner().run("late_day_high_fade", start=datetime(2026, 1, 1, tzinfo=timezone.utc).date(), end=datetime(2026, 1, 2, tzinfo=timezone.utc).date())
    assert result["real_replay_data"] is False
    assert "missing replay snapshots" in result["summary"]
