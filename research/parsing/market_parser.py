from __future__ import annotations

from parsing.rule_parser import (
    detect_city,
    detect_contract_terms,
    detect_settlement_source,
    detect_station,
    detect_variable,
    infer_local_date,
    parse_datetime,
)
from parsing.weather_contract import PARSER_VERSION, WeatherContract


class WeatherMarketParser:
    """Parse Kalshi weather market metadata into a tradability-gated contract."""

    def parse(self, market: dict) -> WeatherContract:
        title = str(market.get("title") or "")
        parts = [
            title,
            str(market.get("subtitle") or ""),
            str(market.get("yes_sub_title") or ""),
            str(market.get("no_sub_title") or ""),
            str(market.get("rules_primary") or ""),
            str(market.get("rules_secondary") or ""),
            str(market.get("functional_strike") or ""),
        ]
        text = " ".join(part for part in parts if part).strip()
        rules = "\n".join(
            str(market.get(key) or "")
            for key in ("rules_primary", "rules_secondary")
            if market.get(key)
        )

        warnings: list[str] = []
        variable_type, variable_warnings = detect_variable(text)
        warnings.extend(variable_warnings)
        terms = detect_contract_terms(market, text)
        contract_type = terms["contract_type"]
        threshold = terms["threshold"]
        comparator = terms["comparator"]
        range_low = terms["range_low"]
        range_high = terms["range_high"]
        unit = terms["unit"]
        if terms.get("warning"):
            warnings.append(str(terms["warning"]))
        city = detect_city(text)
        station_code, station_confidence = detect_station(text)
        local_date = infer_local_date(market, text)
        settlement_source = detect_settlement_source(text)
        close_time = parse_datetime(market.get("close_time"))
        expiration_time = parse_datetime(market.get("expiration_time") or market.get("latest_expiration_time"))

        if not city:
            warnings.append("city could not be parsed")
        if variable_type == "unknown":
            warnings.append("variable type unsupported or unclear")
        if contract_type == "unknown":
            warnings.append("contract type/condition could not be parsed")
        if contract_type in {"threshold_above", "threshold_below"} and (threshold is None or comparator == "unknown"):
            warnings.append("threshold/comparator could not be parsed")
        if contract_type == "range_bucket" and (range_low is None or range_high is None):
            warnings.append("range bucket bounds could not be parsed")
        if not local_date:
            warnings.append("contract local date could not be inferred")
        if not station_code:
            warnings.append("settlement station not found in rules; station mapping must confirm before trading")
        if not settlement_source:
            warnings.append("settlement source not explicit in rules")

        confidence = 0.15
        confidence += 0.2 if variable_type in {"high_temp", "low_temp"} else 0.0
        confidence += 0.2 if contract_type in {"threshold_above", "threshold_below", "range_bucket"} else 0.0
        confidence += 0.15 if city else 0.0
        confidence += 0.15 if local_date else 0.0
        confidence += 0.1 if settlement_source else 0.0
        confidence += 0.05 if station_code else 0.0
        confidence = min(confidence, 0.99)

        yes_condition = ""
        if variable_type != "unknown" and contract_type == "range_bucket" and range_low is not None and range_high is not None:
            yes_condition = f"{variable_type} between {range_low:g}-{range_high:g}{unit or ''}"
        elif variable_type != "unknown" and threshold is not None and comparator != "unknown":
            yes_condition = f"{variable_type} {comparator} {threshold:g}{unit or ''}"

        is_tradable = (
            confidence >= 0.75
            and variable_type in {"high_temp", "low_temp"}
            and (
                (contract_type in {"threshold_above", "threshold_below"} and threshold is not None and comparator in {"gte", "gt", "lte", "lt"})
                or (contract_type == "range_bucket" and range_low is not None and range_high is not None)
            )
            and local_date is not None
            and settlement_source is not None
        )

        return WeatherContract(
            event_ticker=str(market.get("event_ticker") or ""),
            market_ticker=str(market.get("ticker") or ""),
            title=title,
            rules=rules,
            city=city,
            station_code=station_code,
            local_date=local_date,
            variable_type=variable_type,  # type: ignore[arg-type]
            contract_type=contract_type,  # type: ignore[arg-type]
            threshold=threshold,
            comparator=comparator,  # type: ignore[arg-type]
            range_low=range_low,
            range_high=range_high,
            range_inclusive_low=bool(terms["range_inclusive_low"]),
            range_inclusive_high=bool(terms["range_inclusive_high"]),
            unit=unit,
            settlement_source=settlement_source,
            close_time=close_time,
            expiration_time=expiration_time,
            yes_condition=yes_condition,
            yes_condition_text=yes_condition,
            parser_version=PARSER_VERSION,
            parse_confidence=confidence,
            station_confidence=station_confidence,
            is_tradable=is_tradable,
            warnings=sorted(set(warnings)),
        )
