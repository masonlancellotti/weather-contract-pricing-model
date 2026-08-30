from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


VariableType = Literal["high_temp", "low_temp", "precipitation", "snowfall", "wind", "unknown"]
Comparator = Literal["gte", "gt", "lte", "lt", "between", "unknown"]
ContractType = Literal["threshold_above", "threshold_below", "range_bucket", "unknown"]

PARSER_VERSION = "v2_range_bucket_semantics"


class WeatherContract(BaseModel):
    platform: str = "kalshi"
    event_ticker: str
    market_ticker: str
    title: str = ""
    rules: str = ""
    city: str | None = None
    station_code: str | None = None
    local_date: date | None = None
    variable_type: VariableType = "unknown"
    contract_type: ContractType = "unknown"
    threshold: float | None = None
    comparator: Comparator = "unknown"
    range_low: float | None = None
    range_high: float | None = None
    range_inclusive_low: bool = True
    range_inclusive_high: bool = True
    unit: str | None = None
    settlement_source: str | None = None
    close_time: datetime | None = None
    expiration_time: datetime | None = None
    yes_condition: str = ""
    yes_condition_text: str = ""
    parser_version: str = PARSER_VERSION
    parse_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    station_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    is_tradable: bool = False
    warnings: list[str] = Field(default_factory=list)

    def not_tradable_reason(self) -> str | None:
        if self.is_tradable:
            return None
        if self.warnings:
            return "; ".join(self.warnings)
        return "contract failed tradability gates"
