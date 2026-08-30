from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal


Action = Literal["WATCH", "SKIP", "BUY_YES", "BUY_NO", "SELL_YES", "SELL_NO", "PAPER", "TRADE_CANDIDATE"]


@dataclass(frozen=True)
class TradeSignal:
    market_ticker: str
    strategy: str
    action: Action
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    yes_fair_cents: float | None = None
    edge_cents: float | None = None
    confidence: float = 0.0
    quantity: float = 1.0
    reason: str = ""
    skip_reason: str | None = None
    paired_market_ticker: str | None = None
    risk_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp.isoformat()
        return payload


class Strategy:
    name = "base"

    def generate(self, *args, **kwargs) -> TradeSignal:
        raise NotImplementedError
