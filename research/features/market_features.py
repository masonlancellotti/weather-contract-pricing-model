from __future__ import annotations

from backtest.execution import NormalizedOrderBook, dollars_to_cents, fp_to_float


def build_market_features(market: dict, orderbook: NormalizedOrderBook | None = None) -> dict:
    features = {
        "yes_bid": dollars_to_cents(market.get("yes_bid_dollars") or market.get("yes_bid")),
        "yes_ask": dollars_to_cents(market.get("yes_ask_dollars") or market.get("yes_ask")),
        "no_bid": dollars_to_cents(market.get("no_bid_dollars") or market.get("no_bid")),
        "no_ask": dollars_to_cents(market.get("no_ask_dollars") or market.get("no_ask")),
        "volume": fp_to_float(market.get("volume_fp") or market.get("volume")),
        "open_interest": fp_to_float(market.get("open_interest_fp") or market.get("open_interest")),
        "last_trade_price": dollars_to_cents(market.get("last_price_dollars") or market.get("last_price")),
        "last_trade_age_seconds": None,
        "price_change_5m": None,
        "price_change_30m": None,
        "price_change_1h": None,
    }
    if orderbook:
        features.update(orderbook.to_features())
    if features["yes_bid"] is not None and features["yes_ask"] is not None:
        features["yes_mid"] = (features["yes_bid"] + features["yes_ask"]) / 2
        features["spread"] = features["yes_ask"] - features["yes_bid"]
        features["spread_pct"] = features["spread"] / features["yes_mid"] if features["yes_mid"] else None
    else:
        features.setdefault("yes_mid", None)
        features.setdefault("spread", None)
        features.setdefault("spread_pct", None)
    features["market_implied_probability"] = features.get("yes_mid")
    features.setdefault("depth_at_best_bid", 0.0)
    features.setdefault("depth_at_best_ask", 0.0)
    features.setdefault("liquidity_score", min(features["depth_at_best_bid"], features["depth_at_best_ask"]))
    return features
