from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from data.nws_climate_report_client import NWSClimateReportClient
from data.storage import Storage
from data.weather_client import WeatherClient, WeatherObservation
from data.weather_station_mapper import StationMapper
from parsing.weather_contract import WeatherContract

SETTLEMENT_VERSION = "v2_range_bucket_semantics"


@dataclass(frozen=True)
class SettlementBuildResult:
    labels: int
    skipped: int
    warnings: list[str]

    def to_dict(self) -> dict:
        return {"labels": self.labels, "skipped": self.skipped, "warnings": self.warnings}


class WeatherSettlementLoader:
    def __init__(self, storage: Storage | None = None, weather_client: WeatherClient | None = None):
        self.storage = storage or Storage()
        self.weather_client = weather_client or WeatherClient()
        self.climate_client = NWSClimateReportClient(storage=self.storage)
        self.mapper = StationMapper()

    def build_settlements(self, start: date | None = None, end: date | None = None, market_ticker: str | None = None) -> SettlementBuildResult:
        self.storage.init_db()
        contracts = self._contracts(start, end, market_ticker)
        labels = 0
        skipped = 0
        warnings: list[str] = []
        for contract in contracts:
            row = self._label_contract(contract)
            if row is None:
                skipped += 1
                warnings.append(f"{contract.market_ticker}: no settlement label built")
                continue
            self.storage.upsert_settlement_label(row)
            labels += 1
        return SettlementBuildResult(labels=labels, skipped=skipped, warnings=warnings[:50])

    def _contracts(self, start: date | None, end: date | None, market_ticker: str | None) -> list[WeatherContract]:
        frame = self.storage.fetch_table("parsed_contracts", limit=100000)
        if frame.empty:
            return []
        rows = []
        seen: set[str] = set()
        for _, row in frame.sort_values("id", ascending=False).iterrows():
            payload = row["payload"]
            if not isinstance(payload, dict):
                continue
            contract = WeatherContract.model_validate(payload)
            if contract.market_ticker in seen:
                continue
            seen.add(contract.market_ticker)
            if market_ticker and contract.market_ticker != market_ticker:
                continue
            if contract.variable_type not in {"high_temp", "low_temp"} or contract.local_date is None:
                continue
            if start and contract.local_date < start:
                continue
            if end and contract.local_date > end:
                continue
            rows.append(contract)
        return rows

    def _label_contract(self, contract: WeatherContract) -> dict | None:
        if contract.local_date is None:
            return None
        if contract.contract_type == "unknown":
            return None
        if contract.contract_type in {"threshold_above", "threshold_below"} and contract.threshold is None:
            return None
        if contract.contract_type == "range_bucket" and (contract.range_low is None or contract.range_high is None):
            return None
        mapping = self.mapper.resolve(contract.city, contract.station_code)
        if mapping is None:
            return None
        exact_report = self.climate_client.fetch_report(mapping.station_code, contract.local_date, persist=True)
        observations = self.weather_client.historical_hourly_observations(mapping.station_code, contract.local_date, mapping.timezone)
        temps = [obs.temp_f for obs in observations if obs.temp_f is not None]
        warnings: list[str] = ["Settlement computed from hourly station observations, may differ from final NWS climate report."]
        fallback_value: float | None = None
        if temps:
            fallback_value = max(temps) if contract.variable_type == "high_temp" else min(temps)

        exact_value = _exact_value_for_contract(contract, exact_report)
        if exact_value is not None and exact_report.found_exact_date:
            exact_yes = evaluate_contract_result(contract, exact_value)
            diff = exact_value - fallback_value if fallback_value is not None else None
            exact_warnings = ["Exact NWS Daily Climate Report parsed and used for settlement label."]
            if diff is not None and abs(diff) > 0.01:
                exact_warnings.append(f"exact NWS report differs from hourly fallback by {diff:.2f}F")
            return _label_row(
                contract,
                mapping.station_code,
                exact_value,
                exact_yes,
                observations,
                min(1.0, max(0.95, exact_report.parsed.confidence if exact_report.parsed else 0.95)),
                exact_warnings,
                exact_source_available=1,
                exact_source_type="nws_daily_climate_report",
                exact_source_report_id=exact_report.report_product_id,
                fallback_source_type="hourly_station_observations" if fallback_value is not None else None,
                fallback_settlement_value=fallback_value,
                exact_vs_fallback_diff=diff,
            )

        warnings.append("Fallback label from hourly station observations, not exact NWS Daily Climate Report.")
        if exact_report.warnings:
            warnings.extend(exact_report.warnings)
        if len(temps) < 3:
            warnings.append("fewer than 3 temperature observations available")
            return _label_row(contract, mapping.station_code, None, None, observations, 0.0, warnings)
        if len(temps) < 18:
            warnings.append(f"partial observation day: {len(temps)} temperature observations")
        settlement_value = fallback_value
        if settlement_value is None:
            return None
        yes_result = evaluate_contract_result(contract, settlement_value)
        confidence = mapping.confidence
        if contract.station_code:
            confidence = min(max(confidence, 0.75), 0.8)
        else:
            confidence = min(confidence, 0.75)
            warnings.append("station mapping inferred, not explicitly parsed from rules")
        if len(temps) < 18:
            confidence = min(confidence, 0.7)
        return _label_row(
            contract,
            mapping.station_code,
            settlement_value,
            yes_result,
            observations,
            confidence,
            warnings,
            exact_source_available=0,
            exact_source_type=None,
            exact_source_report_id=exact_report.report_product_id,
            fallback_source_type="hourly_station_observations",
            fallback_settlement_value=settlement_value,
            exact_vs_fallback_diff=None,
        )


def evaluate_condition(value: float, threshold: float, comparator: str) -> int:
    if comparator == "gt":
        return int(value > threshold)
    if comparator == "gte":
        return int(value >= threshold)
    if comparator == "lt":
        return int(value < threshold)
    if comparator == "lte":
        return int(value <= threshold)
    raise ValueError(f"Unsupported comparator for settlement label: {comparator}")


def evaluate_contract_result(contract: WeatherContract, settlement_value: float) -> int | None:
    if contract.contract_type == "threshold_above":
        if contract.threshold is None:
            return None
        if contract.comparator == "gt":
            return int(settlement_value > contract.threshold)
        if contract.comparator == "gte":
            return int(settlement_value >= contract.threshold)
    if contract.contract_type == "threshold_below":
        if contract.threshold is None:
            return None
        if contract.comparator == "lt":
            return int(settlement_value < contract.threshold)
        if contract.comparator == "lte":
            return int(settlement_value <= contract.threshold)
    if contract.contract_type == "range_bucket":
        if contract.range_low is None or contract.range_high is None:
            return None
        low_ok = settlement_value >= contract.range_low if contract.range_inclusive_low else settlement_value > contract.range_low
        high_ok = settlement_value <= contract.range_high if contract.range_inclusive_high else settlement_value < contract.range_high
        return int(low_ok and high_ok)
    return None


def _label_row(
    contract: WeatherContract,
    station_code: str,
    settlement_value: float | None,
    yes_result: int | None,
    observations: list[WeatherObservation],
    confidence: float,
    warnings: list[str],
    exact_source_available: int = 0,
    exact_source_type: str | None = None,
    exact_source_report_id: str | None = None,
    fallback_source_type: str | None = None,
    fallback_settlement_value: float | None = None,
    exact_vs_fallback_diff: float | None = None,
) -> dict:
    raw = {
        "observations_count": len(observations),
        "observation_sources": sorted({obs.source for obs in observations}),
        "contract": contract.model_dump(mode="json"),
    }
    primary_source_type = exact_source_type if exact_source_available and exact_source_type else fallback_source_type or "unknown"
    return {
        "market_ticker": contract.market_ticker,
        "event_ticker": contract.event_ticker,
        "city": contract.city,
        "station_code": station_code,
        "local_date": contract.local_date.isoformat() if contract.local_date else None,
        "variable_type": contract.variable_type,
        "contract_type": contract.contract_type,
        "threshold": contract.threshold,
        "comparator": contract.comparator,
        "range_low": contract.range_low,
        "range_high": contract.range_high,
        "unit": contract.unit,
        "settlement_value": settlement_value,
        "yes_result": yes_result,
        "source": primary_source_type,
        "primary_source_type": primary_source_type,
        "confidence": confidence,
        "warnings": "; ".join(warnings),
        "raw_json": json.dumps(raw, default=str),
        "exact_source_available": exact_source_available,
        "exact_source_type": exact_source_type,
        "exact_source_report_id": exact_source_report_id,
        "exact_settlement_value": settlement_value if exact_source_available else None,
        "fallback_source_type": fallback_source_type,
        "fallback_settlement_value": fallback_settlement_value,
        "exact_vs_fallback_diff": exact_vs_fallback_diff,
        "settlement_version": SETTLEMENT_VERSION,
    }


def _exact_value_for_contract(contract: WeatherContract, report) -> float | None:
    if not report or not report.parsed:
        return None
    if contract.variable_type == "high_temp":
        return report.parsed.high_temp
    if contract.variable_type == "low_temp":
        return report.parsed.low_temp
    return None
