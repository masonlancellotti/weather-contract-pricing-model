from datetime import datetime, timezone

from backtest.execution import NormalizedOrderBook
from live.orderbook_recorder import normalize_live_orderbook_snapshot


def test_live_orderbook_snapshot_math_and_depth():
    raw = {"orderbook_fp": {"yes_dollars": [["0.42", "10"], ["0.40", "5"]], "no_dollars": [["0.53", "7"], ["0.50", "3"]]}}
    book = NormalizedOrderBook.from_kalshi("T", raw)
    row = normalize_live_orderbook_snapshot("T", datetime(2026, 1, 1, tzinfo=timezone.utc), book, raw)
    assert row["yes_best_bid"] == 42
    assert row["yes_best_ask"] == 47
    assert row["no_best_bid"] == 53
    assert row["no_best_ask"] == 58
    assert row["spread_cents"] == 5
    assert row["mid_cents"] == 44.5
    assert row["depth_yes_bid_1"] == 10
    assert row["depth_yes_ask_1"] == 7
    assert row["total_yes_bid_depth"] == 15
    assert row["total_no_bid_depth"] == 10
