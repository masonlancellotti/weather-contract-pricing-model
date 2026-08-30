from __future__ import annotations

from dataclasses import dataclass, field
from math import floor
from typing import Iterable


def dollars_to_cents(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if 0 <= numeric <= 1:
        return int(round(numeric * 100))
    return int(round(numeric))


def fp_to_float(value: object) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class PriceLevel:
    price_cents: int
    size: float


@dataclass
class NormalizedOrderBook:
    ticker: str
    yes_bids: list[PriceLevel] = field(default_factory=list)
    no_bids: list[PriceLevel] = field(default_factory=list)

    @classmethod
    def from_kalshi(cls, ticker: str, raw: dict) -> "NormalizedOrderBook":
        book = raw.get("orderbook_fp") or raw.get("orderbook") or raw
        yes_raw = book.get("yes_dollars") or book.get("yes") or []
        no_raw = book.get("no_dollars") or book.get("no") or []
        return cls(
            ticker=ticker,
            yes_bids=_normalize_levels(yes_raw),
            no_bids=_normalize_levels(no_raw),
        )

    @property
    def yes_bid(self) -> int | None:
        return self.yes_bids[0].price_cents if self.yes_bids else None

    @property
    def no_bid(self) -> int | None:
        return self.no_bids[0].price_cents if self.no_bids else None

    @property
    def yes_ask(self) -> int | None:
        return 100 - self.no_bid if self.no_bid is not None else None

    @property
    def no_ask(self) -> int | None:
        return 100 - self.yes_bid if self.yes_bid is not None else None

    @property
    def spread(self) -> int | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    @property
    def mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def depth_at_best_bid(self) -> float:
        return self.yes_bids[0].size if self.yes_bids else 0.0

    @property
    def depth_at_best_ask(self) -> float:
        return self.no_bids[0].size if self.no_bids else 0.0

    def available_depth(self, side: str, price_cents: int) -> float:
        if side == "buy_yes":
            levels = self.no_bids
            return sum(level.size for level in levels if 100 - level.price_cents <= price_cents)
        if side == "buy_no":
            levels = self.yes_bids
            return sum(level.size for level in levels if 100 - level.price_cents <= price_cents)
        if side == "sell_yes":
            return sum(level.size for level in self.yes_bids if level.price_cents >= price_cents)
        if side == "sell_no":
            return sum(level.size for level in self.no_bids if level.price_cents >= price_cents)
        return 0.0

    def to_features(self) -> dict:
        spread = self.spread
        mid = self.mid
        return {
            "yes_bid": self.yes_bid,
            "yes_ask": self.yes_ask,
            "no_bid": self.no_bid,
            "no_ask": self.no_ask,
            "yes_mid": mid,
            "spread": spread,
            "spread_pct": (spread / mid) if spread is not None and mid else None,
            "depth_at_best_bid": self.depth_at_best_bid,
            "depth_at_best_ask": self.depth_at_best_ask,
            "liquidity_score": min(self.depth_at_best_bid, self.depth_at_best_ask),
        }


def _normalize_levels(raw_levels: Iterable) -> list[PriceLevel]:
    levels: list[PriceLevel] = []
    for row in raw_levels:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        cents = dollars_to_cents(row[0])
        if cents is None:
            continue
        levels.append(PriceLevel(price_cents=cents, size=fp_to_float(row[1])))
    levels.sort(key=lambda level: level.price_cents, reverse=True)
    return levels


def floor_quote(price_cents: float) -> int:
    return max(1, min(99, floor(price_cents)))
