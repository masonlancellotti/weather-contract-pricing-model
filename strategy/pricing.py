"""
strategy/pricing.py - Convert model output to fair prices and edge calculations.

Edge = fair_probability - market_probability for the side we would buy.
Common-sense guardrails live here too so impossible or nearly locked buckets do
not turn into paper "opportunities".
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from config import cfg
from models import MarketSnapshot, ModelOutput, Side, TradeSignal, Venue
from utils.time import city_local_now

KALSHI_FEE_RATE = 0.02
POLY_FEE_RATE = 0.02
SLIPPAGE_BUFFER = 0.01
MODEL_UNCERTAINTY_BUFFER = 0.02


@dataclass
class MarketAnalysis:
    adjusted_yes_fair: float
    yes_buy_prob: Optional[float]
    no_buy_prob: Optional[float]
    yes_edge: float
    no_edge: float
    threshold: float
    best_side: Optional[Side]
    best_edge: float
    best_market_prob: Optional[float]
    best_side_fair: Optional[float]
    guardrail_notes: list[str]


def compute_edge(
    snapshot: MarketSnapshot,
    model: ModelOutput,
) -> Optional[TradeSignal]:
    """
    Compare model fair probability to executable market prices.
    YES uses the current yes ask.
    NO uses the current no ask, which is (1 - yes bid).
    """
    analysis = analyze_market(snapshot, model)
    if analysis is None or analysis.best_side is None or analysis.best_edge < analysis.threshold:
        return None

    if analysis.best_side == Side.YES:
        side = Side.YES
        edge = analysis.yes_edge
        market_prob = analysis.yes_buy_prob
        suggested_price = max(
            (snapshot.best_ask or Decimal("0.01")) - Decimal("0.01"),
            (snapshot.best_bid + Decimal("0.01")) if snapshot.best_bid is not None else Decimal("0.01"),
        )
        suggested_price = max(Decimal("0.01"), min(Decimal("0.99"), suggested_price))
    else:
        side = Side.NO
        edge = analysis.no_edge
        market_prob = analysis.no_buy_prob
        no_best_bid = (Decimal("1.00") - snapshot.best_ask) if snapshot.best_ask is not None else None
        suggested_price = (Decimal(str(analysis.no_buy_prob)) - Decimal("0.01")) if analysis.no_buy_prob is not None else Decimal("0.01")
        if no_best_bid is not None:
            suggested_price = max(suggested_price, no_best_bid + Decimal("0.01"))
        suggested_price = max(Decimal("0.01"), min(Decimal("0.99"), suggested_price))

    contracts = 1
    side_fair = analysis.adjusted_yes_fair if side == Side.YES else (1 - analysis.adjusted_yes_fair)
    reason_parts = [
        f"yes_fair={analysis.adjusted_yes_fair:.3f}",
        f"side_fair={side_fair:.3f}",
        f"market={market_prob:.3f}",
        f"edge={edge:.3f}",
        f"threshold={analysis.threshold:.3f}",
        f"conf={model.confidence:.2f}",
    ]
    reason_parts.extend(analysis.guardrail_notes)

    return TradeSignal(
        venue=snapshot.venue,
        market_id=snapshot.market_id,
        city=snapshot.city,
        bucket_label=snapshot.bucket_label,
        side=side,
        fair_prob=analysis.adjusted_yes_fair,
        market_prob=market_prob,
        edge=edge,
        suggested_price=suggested_price,
        suggested_contracts=contracts,
        reason=" ".join(reason_parts),
    )


def analyze_market(
    snapshot: MarketSnapshot,
    model: ModelOutput,
) -> Optional[MarketAnalysis]:
    fair_prob = model.bucket_probs.get(snapshot.bucket_label)
    if fair_prob is None:
        return None

    adjusted_fair = fair_prob * model.confidence + 0.5 * (1 - model.confidence)
    yes_buy_prob = float(snapshot.best_ask) if snapshot.best_ask is not None else None
    no_buy_prob = (1 - float(snapshot.best_bid)) if snapshot.best_bid is not None else None

    yes_allowed, no_allowed, guardrail_notes = _trade_guardrails(snapshot, model, yes_buy_prob, no_buy_prob)

    fee_buffer = (
        KALSHI_FEE_RATE if snapshot.venue == Venue.KALSHI else POLY_FEE_RATE
    ) + SLIPPAGE_BUFFER + MODEL_UNCERTAINTY_BUFFER
    threshold = cfg.min_edge_threshold + fee_buffer

    yes_edge = adjusted_fair - yes_buy_prob if yes_buy_prob is not None and yes_allowed else float("-inf")
    no_edge = (1 - adjusted_fair) - no_buy_prob if no_buy_prob is not None and no_allowed else float("-inf")

    best_side = None
    best_edge = float("-inf")
    best_market_prob = None
    best_side_fair = None
    if yes_edge >= no_edge and yes_buy_prob is not None and yes_allowed:
        best_side = Side.YES
        best_edge = yes_edge
        best_market_prob = yes_buy_prob
        best_side_fair = adjusted_fair
    elif no_buy_prob is not None and no_allowed:
        best_side = Side.NO
        best_edge = no_edge
        best_market_prob = no_buy_prob
        best_side_fair = 1 - adjusted_fair

    return MarketAnalysis(
        adjusted_yes_fair=adjusted_fair,
        yes_buy_prob=yes_buy_prob,
        no_buy_prob=no_buy_prob,
        yes_edge=yes_edge,
        no_edge=no_edge,
        threshold=threshold,
        best_side=best_side,
        best_edge=best_edge,
        best_market_prob=best_market_prob,
        best_side_fair=best_side_fair,
        guardrail_notes=guardrail_notes,
    )


def _trade_guardrails(
    snapshot: MarketSnapshot,
    model: ModelOutput,
    yes_buy_prob: Optional[float],
    no_buy_prob: Optional[float],
) -> tuple[bool, bool, list[str]]:
    today_local = city_local_now(snapshot.city).date().isoformat()
    if snapshot.settlement_date != today_local:
        return True, True, []

    slack = cfg.bucket_guardrail_slack_f
    observed_floor_candidates = [
        value for value in (model.observed_high_so_far, model.obs_temp) if value is not None
    ]
    observed_floor = max(observed_floor_candidates) if observed_floor_candidates else None
    ceiling_candidates = [
        value for value in (model.remaining_forecast_high, model.observed_high_so_far, model.obs_temp)
        if value is not None
    ]
    plausible_ceiling = max(ceiling_candidates) if ceiling_candidates else None

    yes_impossible = False
    no_impossible = False
    notes: list[str] = []

    if observed_floor is not None and snapshot.bucket_max is not None:
        if observed_floor > snapshot.bucket_max + slack:
            yes_impossible = True
            notes.append("guard=already_above_bucket")

    if plausible_ceiling is not None and snapshot.bucket_min is not None:
        if plausible_ceiling < snapshot.bucket_min - slack:
            yes_impossible = True
            notes.append("guard=cannot_reach_bucket")

    if (
        observed_floor is not None
        and plausible_ceiling is not None
        and snapshot.bucket_min is not None
        and snapshot.bucket_max is not None
        and snapshot.bucket_min - slack <= observed_floor <= snapshot.bucket_max + slack
        and plausible_ceiling <= snapshot.bucket_max + slack
    ):
        no_impossible = True
        notes.append("guard=bucket_locked_in")

    local_hour = model.local_hour if model.local_hour is not None else city_local_now(snapshot.city).hour
    certainty = cfg.late_day_market_certainty_prob
    if local_hour >= cfg.late_day_guard_local_hour:
        if yes_buy_prob is not None and yes_buy_prob >= certainty and not yes_impossible:
            no_impossible = True
            notes.append("guard=late_day_no_fade_yes")
        if no_buy_prob is not None and no_buy_prob >= certainty and not no_impossible:
            yes_impossible = True
            notes.append("guard=late_day_no_fade_no")

    return (not yes_impossible), (not no_impossible), notes
