from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import pandas as pd


class PassiveFillType(StrEnum):
    NO_FILL = "NO_FILL"
    TOUCHED_ONLY_NO_FILL = "TOUCHED_ONLY_NO_FILL"
    TRADED_THROUGH_FILL = "TRADED_THROUGH_FILL"
    MARKETABLE_FILL = "MARKETABLE_FILL"
    PARTIAL_FILL_CONSERVATIVE = "PARTIAL_FILL_CONSERVATIVE"
    UNKNOWN_SKIP = "UNKNOWN_SKIP"


@dataclass(frozen=True)
class PassiveFillConfig:
    assume_touch_fill: bool = False
    fill_haircut: float = 0.25
    adverse_selection_penalty_cents: float = 2.0
    require_traded_through: bool = True
    min_displayed_depth: float = 5.0
    traded_through_cents: float = 1.0


@dataclass(frozen=True)
class PassiveQuote:
    market_ticker: str
    side: str
    limit_price: float
    quantity: float
    submitted_at: datetime | None = None


@dataclass(frozen=True)
class PassiveFillResult:
    fill_type: PassiveFillType
    fill_price: float | None
    fill_quantity: float
    fill_ts: datetime | None
    approximate: bool
    queue_ahead: float
    adverse_selection_penalty_cents: float
    reason: str

    @property
    def filled(self) -> bool:
        return self.fill_quantity > 0 and self.fill_price is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_type": self.fill_type.value,
            "fill_price": self.fill_price,
            "fill_quantity": self.fill_quantity,
            "fill_ts": self.fill_ts,
            "approximate": self.approximate,
            "queue_ahead": self.queue_ahead,
            "adverse_selection_penalty_cents": self.adverse_selection_penalty_cents,
            "reason": self.reason,
        }


def simulate_passive_fill(quote: PassiveQuote, future_books: pd.DataFrame, cfg: PassiveFillConfig | None = None) -> PassiveFillResult:
    cfg = cfg or PassiveFillConfig()
    if future_books.empty:
        return _result(PassiveFillType.NO_FILL, None, 0.0, None, 0.0, cfg, "No future book rows.")
    side = quote.side.upper()
    touched = False
    queue_ahead = 0.0
    for _, row in future_books.sort_values("ts").iterrows():
        ts = _parse_ts(row.get("ts"))
        if side == "BUY_YES":
            ask = _num(row.get("yes_best_ask"))
            same_depth = _num(row.get("depth_yes_bid_1")) or 0.0
        elif side == "BUY_NO":
            ask = _num(row.get("no_best_ask"))
            same_depth = _num(row.get("depth_yes_ask_1")) or 0.0
        else:
            return _result(PassiveFillType.UNKNOWN_SKIP, None, 0.0, ts, 0.0, cfg, f"Unsupported passive side {quote.side}.")
        if ask is None:
            continue
        if ask < quote.limit_price:
            fill_qty = _haircut_qty(quote.quantity, same_depth, cfg)
            fill_price = min(99.0, quote.limit_price + cfg.adverse_selection_penalty_cents)
            fill_type = PassiveFillType.TRADED_THROUGH_FILL if fill_qty >= quote.quantity else PassiveFillType.PARTIAL_FILL_CONSERVATIVE
            return _result(fill_type, fill_price, fill_qty, ts, same_depth, cfg, "Future book traded through quote; conservative fill counted.")
        if ask == quote.limit_price:
            touched = True
            queue_ahead = max(queue_ahead, same_depth)
            if cfg.assume_touch_fill and not cfg.require_traded_through:
                fill_qty = _haircut_qty(quote.quantity, same_depth, cfg)
                return _result(PassiveFillType.PARTIAL_FILL_CONSERVATIVE, quote.limit_price, fill_qty, ts, same_depth, cfg, "Quote touched; fill counted only because assume_touch_fill=true.")
    if touched:
        return _result(PassiveFillType.TOUCHED_ONLY_NO_FILL, None, 0.0, None, queue_ahead, cfg, "Quote was touched but no traded-through evidence; no fill.")
    return _result(PassiveFillType.NO_FILL, None, 0.0, None, 0.0, cfg, "Quote never touched.")


def adverse_selection_cents(side: str, fill_price: float, future_yes_mid: float | None) -> float | None:
    if future_yes_mid is None:
        return None
    side = side.upper()
    if side == "BUY_YES":
        return future_yes_mid - fill_price
    if side == "BUY_NO":
        future_no_mid = 100.0 - future_yes_mid
        return future_no_mid - fill_price
    return None


def _haircut_qty(quantity: float, queue_ahead: float, cfg: PassiveFillConfig) -> float:
    if queue_ahead < cfg.min_displayed_depth:
        return max(0.0, quantity * cfg.fill_haircut)
    return max(0.0, quantity * cfg.fill_haircut)


def _result(fill_type: PassiveFillType, price: float | None, qty: float, ts: datetime | None, queue: float, cfg: PassiveFillConfig, reason: str) -> PassiveFillResult:
    return PassiveFillResult(
        fill_type=fill_type,
        fill_price=price,
        fill_quantity=qty,
        fill_ts=ts,
        approximate=fill_type not in {PassiveFillType.NO_FILL, PassiveFillType.TOUCHED_ONLY_NO_FILL},
        queue_ahead=queue,
        adverse_selection_penalty_cents=cfg.adverse_selection_penalty_cents,
        reason=reason,
    )


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
