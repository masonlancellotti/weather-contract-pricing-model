from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class ParsedClimateReport:
    station_name: str | None
    report_date: date | None
    issued_at: datetime | None
    high_temp: float | None
    low_temp: float | None
    precip: float | None
    snowfall: float | None
    confidence: float
    warnings: list[str]

    def to_dict(self) -> dict:
        return {
            "station_name": self.station_name,
            "report_date": self.report_date.isoformat() if self.report_date else None,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "high_temp": self.high_temp,
            "low_temp": self.low_temp,
            "precip": self.precip,
            "snowfall": self.snowfall,
            "confidence": self.confidence,
            "warnings": self.warnings,
        }


def parse_cli_report(raw_text: str) -> ParsedClimateReport:
    warnings: list[str] = []
    text = _clean(raw_text)
    high = _extract_temperature(text, "MAXIMUM")
    low = _extract_temperature(text, "MINIMUM")
    precip = _extract_scalar(text, r"PRECIPITATION \(IN\).*?(?:YESTERDAY|TODAY)\s+([TM\d.]+)")
    snowfall = _extract_scalar(text, r"SNOWFALL \(IN\).*?(?:YESTERDAY|TODAY)\s+([TM\d.]+)")
    report_date = _extract_report_date(text)
    issued_at = _extract_issued_at(text)
    station_name = _extract_station_name(text)
    if high is None:
        warnings.append("could not parse daily maximum temperature")
    if low is None:
        warnings.append("could not parse daily minimum temperature")
    if report_date is None:
        warnings.append("could not parse report date")
    confidence = 0.95
    if high is None or low is None:
        confidence = min(confidence, 0.55)
    if report_date is None:
        confidence = min(confidence, 0.75)
    return ParsedClimateReport(station_name, report_date, issued_at, high, low, precip, snowfall, confidence, warnings)


def _clean(raw_text: str) -> str:
    return "\n".join(line.rstrip() for line in raw_text.replace("\r", "").splitlines())


def _extract_temperature(text: str, label: str) -> float | None:
    match = re.search(rf"^\s*{label}\s+(-?\d+|MM|M)\b", text, re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    return _number(match.group(1))


def _extract_scalar(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return _number(match.group(1))


def _extract_report_date(text: str) -> date | None:
    match = re.search(r"THE\s+(.+?)\s+CLIMATE SUMMARY FOR\s+([A-Z]+\s+\d{1,2}\s+\d{4})", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(2).title(), "%B %d %Y").date()
    except ValueError:
        return None


def _extract_station_name(text: str) -> str | None:
    match = re.search(r"THE\s+(.+?)\s+CLIMATE SUMMARY FOR", text, re.IGNORECASE)
    return " ".join(match.group(1).title().split()) if match else None


def _extract_issued_at(text: str) -> datetime | None:
    # CLI timestamps omit the date year zone detail in a way that varies by WFO.
    # Keep this parser conservative and avoid manufacturing a datetime when the
    # product API metadata usually carries issuance time.
    return None


def _number(raw: str) -> float | None:
    raw = raw.strip().upper()
    if raw in {"M", "MM", ""}:
        return None
    if raw == "T":
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return None
