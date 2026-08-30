from backtest.execution import NormalizedOrderBook


def test_kalshi_binary_orderbook_math_from_fixed_point_dollars():
    raw = {
        "orderbook_fp": {
            "yes_dollars": [["0.4200", "15.00"], ["0.4100", "10.00"]],
            "no_dollars": [["0.5300", "20.00"], ["0.5200", "10.00"]],
        }
    }
    book = NormalizedOrderBook.from_kalshi("TEST", raw)
    assert book.yes_bid == 42
    assert book.no_bid == 53
    assert book.yes_ask == 47
    assert book.no_ask == 58
    assert book.spread == 5
    assert book.mid == 44.5


def test_available_depth_for_buy_yes_uses_no_bids_as_yes_asks():
    raw = {"orderbook_fp": {"yes_dollars": [["0.4000", "7.00"]], "no_dollars": [["0.6000", "5.00"], ["0.5800", "8.00"]]}}
    book = NormalizedOrderBook.from_kalshi("TEST", raw)
    assert book.available_depth("buy_yes", 40) == 5
    assert book.available_depth("buy_yes", 42) == 13
