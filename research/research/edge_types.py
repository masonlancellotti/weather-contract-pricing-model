from __future__ import annotations

FAIR_VALUE_TAKER_EDGE = "FAIR_VALUE_TAKER_EDGE"
ALREADY_GUARANTEED_OR_IMPOSSIBLE_EDGE = "ALREADY_GUARANTEED_OR_IMPOSSIBLE_EDGE"
LATE_DAY_FORECAST_MISPRICING_EDGE = "LATE_DAY_FORECAST_MISPRICING_EDGE"
LADDER_RELATIVE_VALUE_EDGE = "LADDER_RELATIVE_VALUE_EDGE"
PASSIVE_LIQUIDITY_SPREAD_EDGE = "PASSIVE_LIQUIDITY_SPREAD_EDGE"
UNKNOWN_OR_UNSUPPORTED = "UNKNOWN_OR_UNSUPPORTED"

SIGNAL_ONLY = "signal_only"
TAKER = "taker"
CONSERVATIVE_PASSIVE = "conservative_passive"
FULL_ORDERBOOK_PASSIVE_APPROX = "full_orderbook_passive_approx"
LIVE_PAPER = "live_paper"


def confidence_level(score: float | None) -> str:
    value = 0.0 if score is None else float(score)
    if value >= 0.8:
        return "high"
    if value >= 0.55:
        return "medium"
    return "low"


def edge_type_for_strategy(strategy: str) -> str:
    strategy = (strategy or "").lower()
    if "already" in strategy:
        return ALREADY_GUARANTEED_OR_IMPOSSIBLE_EDGE
    if "late_day" in strategy:
        return LATE_DAY_FORECAST_MISPRICING_EDGE
    if "ladder" in strategy:
        return LADDER_RELATIVE_VALUE_EDGE
    if "passive" in strategy or "wide_spread" in strategy:
        return PASSIVE_LIQUIDITY_SPREAD_EDGE
    if "fair" in strategy or "rank" in strategy:
        return FAIR_VALUE_TAKER_EDGE
    return UNKNOWN_OR_UNSUPPORTED
