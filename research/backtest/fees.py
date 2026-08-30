from __future__ import annotations

from dataclasses import dataclass


class FeeModel:
    name = "base"

    def fee_cents(self, price_cents: int, quantity: float = 1.0) -> float:
        raise NotImplementedError


@dataclass(frozen=True)
class ConservativeFixedFeeModel(FeeModel):
    per_contract_cents: float = 1.0
    name: str = "conservative_fixed"

    def fee_cents(self, price_cents: int, quantity: float = 1.0) -> float:
        return self.per_contract_cents * quantity


@dataclass(frozen=True)
class PercentageFeeModel(FeeModel):
    rate: float = 0.02
    minimum_cents: float = 0.5
    name: str = "percentage"

    def fee_cents(self, price_cents: int, quantity: float = 1.0) -> float:
        return max(self.minimum_cents * quantity, price_cents * self.rate * quantity)


class NoFeeModel(FeeModel):
    name = "no_fee_comparison_only"

    def fee_cents(self, price_cents: int, quantity: float = 1.0) -> float:
        return 0.0
