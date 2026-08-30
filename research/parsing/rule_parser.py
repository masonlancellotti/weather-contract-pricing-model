from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo


STATION_ALIASES = {
    "central park": "KNYC",
    "laguardia": "KLGA",
    "jfk": "KJFK",
    "newark": "KEWR",
    "hartsfield": "KATL",
    "ohare": "KORD",
    "o'hare": "KORD",
    "midway": "KMDW",
    "lax": "KLAX",
}


CITY_PATTERNS = {
    "Atlanta": [r"\batlanta\b"],
    "New York": [r"\bnew york\b", r"\bnyc\b", r"\bmanhattan\b"],
    "Los Angeles": [r"\blos angeles\b", r"\bla\b"],
    "Chicago": [r"\bchicago\b"],
    "Miami": [r"\bmiami\b"],
    "Dallas": [r"\bdallas\b", r"\bfort worth\b", r"\bdfw\b"],
    "Philadelphia": [r"\bphiladelphia\b"],
    "Boston": [r"\bboston\b"],
    "Washington DC": [r"\bwashington,?\s+d\.?c\.?\b", r"\bdc\b"],
    "Houston": [r"\bhouston\b"],
    "Phoenix": [r"\bphoenix\b"],
    "Seattle": [r"\bseattle\b"],
    "Denver": [r"\bdenver\b"],
    "Las Vegas": [r"\blas vegas\b"],
    "San Francisco": [r"\bsan francisco\b"],
    "Austin": [r"\baustin\b", r"\baus\b"],
}


def parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def infer_local_date(market: dict, text: str) -> date | None:
    for key in ("occurrence_datetime", "expected_expiration_time", "expiration_time", "close_time"):
        parsed = parse_datetime(market.get(key))
        if parsed:
            return parsed.astimezone(ZoneInfo("America/New_York")).date()

    # Conservative fallback for common "on Apr 27" style titles. The year is
    # intentionally omitted because trading decisions should prefer API dates.
    month_re = (
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
        r"[a-z]*\.?\s+(\d{1,2})\b"
    )
    match = re.search(month_re, text, re.IGNORECASE)
    if match:
        month_lookup = {
            "jan": 1,
            "feb": 2,
            "mar": 3,
            "apr": 4,
            "may": 5,
            "jun": 6,
            "jul": 7,
            "aug": 8,
            "sep": 9,
            "sept": 9,
            "oct": 10,
            "nov": 11,
            "dec": 12,
        }
        return date(datetime.now().year, month_lookup[match.group(1).lower()[:3]], int(match.group(2)))
    return None


def detect_city(text: str) -> str | None:
    lowered = text.lower()
    for city, patterns in CITY_PATTERNS.items():
        if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in patterns):
            return city
    in_match = re.search(r"\bin\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2})\b", text)
    if in_match:
        candidate = in_match.group(1).strip()
        if candidate.lower() not in {"the", "a", "an"}:
            return candidate
    return None


def detect_station(text: str) -> tuple[str | None, float]:
    code_match = re.search(r"\bK[A-Z0-9]{3}\b", text.upper())
    if code_match:
        return code_match.group(0), 0.95
    lowered = text.lower()
    for alias, code in STATION_ALIASES.items():
        if alias in lowered:
            return code, 0.9
    return None, 0.0


def detect_variable(text: str) -> tuple[str, list[str]]:
    lowered = text.lower()
    warnings: list[str] = []
    if any(term in lowered for term in ["highest temperature", "high temperature", "high temp", "daily high", "maximum temperature"]):
        return "high_temp", warnings
    if any(term in lowered for term in ["lowest temperature", "low temperature", "low temp", "daily low", "minimum temperature"]):
        return "low_temp", warnings
    if "temperature" in lowered or "temp" in lowered:
        warnings.append("temperature market detected but high/low side is unclear")
        return "unknown", warnings
    if "precip" in lowered or "rain" in lowered:
        return "precipitation", warnings
    if "snow" in lowered:
        return "snowfall", warnings
    if "wind" in lowered:
        return "wind", warnings
    warnings.append("not recognized as a supported weather variable")
    return "unknown", warnings


def detect_threshold_and_comparator(market: dict, text: str) -> tuple[float | None, str, str | None]:
    for key in ("floor_strike", "cap_strike"):
        value = market.get(key)
        if value not in (None, ""):
            try:
                strike_type = str(market.get("strike_type") or "").lower()
                comparator = "gt" if strike_type in {"greater", "above", "gt"} else "lt"
                if strike_type in {"gte", "greater_or_equal"}:
                    comparator = "gte"
                if strike_type in {"lte", "less_or_equal"}:
                    comparator = "lte"
                return float(value), comparator, "F"
            except (TypeError, ValueError):
                pass

    patterns = [
        (r"(?:at\s+least|at\s+or\s+above|greater\s+than\s+or\s+equal\s+to|no\s+less\s+than|>=)\s*(\d{1,3}(?:\.\d+)?)", "gte"),
        (r"(?:above|over|greater\s+than|more\s+than|>)\s*(\d{1,3}(?:\.\d+)?)", "gt"),
        (r"(\d{1,3}(?:\.\d+)?)\s*(?:degrees?|deg|°)?\s*(?:f|fahrenheit)?\s*(?:or\s+higher|or\s+above|and\s+above)", "gte"),
        (r"(?:at\s+most|at\s+or\s+below|less\s+than\s+or\s+equal\s+to|no\s+more\s+than|<=)\s*(\d{1,3}(?:\.\d+)?)", "lte"),
        (r"(?:below|under|less\s+than|fewer\s+than|<)\s*(\d{1,3}(?:\.\d+)?)", "lt"),
        (r"(\d{1,3}(?:\.\d+)?)\s*(?:degrees?|deg|°)?\s*(?:f|fahrenheit)?\s*(?:or\s+lower|or\s+below|and\s+below)", "lte"),
    ]
    for pattern, comparator in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return float(match.group(1)), comparator, "F"

    between = re.search(r"\bbetween\s+(\d{1,3}(?:\.\d+)?)\s+(?:and|-)\s+(\d{1,3}(?:\.\d+)?)", text, re.IGNORECASE)
    if between:
        return float(between.group(1)), "between", "F"
    return None, "unknown", None


def detect_settlement_source(text: str) -> str | None:
    lowered = text.lower()
    if "national weather service" in lowered or "weather.gov" in lowered or "nws" in lowered:
        return "NWS/NOAA"
    if "noaa" in lowered:
        return "NOAA"
    if "airport" in lowered or "asos" in lowered or "metar" in lowered:
        return "ASOS/METAR"
    return None


def detect_contract_terms(market: dict, text: str) -> dict:
    """Parse explicit contract semantics from human-readable title/rules.

    Strike metadata is only a weak fallback. Kalshi bucket titles such as
    "66-67 degrees" must never become synthetic less-than/greater-than trades.
    """
    range_match = _detect_range_bucket(text)
    if range_match:
        low, high = range_match
        return _terms("range_bucket", None, "unknown", "F", low, high)

    threshold = _detect_text_threshold(text)
    if threshold:
        value, comparator = threshold
        contract_type = "threshold_above" if comparator in {"gt", "gte"} else "threshold_below"
        return _terms(contract_type, value, comparator, "F")

    strike = _detect_strike_fallback(market)
    if strike:
        value, comparator = strike
        contract_type = "threshold_above" if comparator in {"gt", "gte"} else "threshold_below"
        terms = _terms(contract_type, value, comparator, "F")
        terms["warning"] = "threshold inferred from strike metadata because title/rules lacked explicit wording"
        return terms

    return _terms("unknown", None, "unknown", None)


def detect_threshold_and_comparator(market: dict, text: str) -> tuple[float | None, str, str | None]:
    terms = detect_contract_terms(market, text)
    if terms["contract_type"] == "range_bucket":
        return None, "between", terms.get("unit")
    return terms.get("threshold"), terms.get("comparator", "unknown"), terms.get("unit")


def _terms(
    contract_type: str,
    threshold: float | None,
    comparator: str,
    unit: str | None,
    range_low: float | None = None,
    range_high: float | None = None,
) -> dict:
    return {
        "contract_type": contract_type,
        "threshold": threshold,
        "comparator": comparator,
        "range_low": range_low,
        "range_high": range_high,
        "range_inclusive_low": True,
        "range_inclusive_high": True,
        "unit": unit,
    }


def _detect_range_bucket(text: str) -> tuple[float, float] | None:
    patterns = [
        r"\bbetween\s+(\d{1,3}(?:\.\d+)?)\s+(?:and|to|-)\s+(\d{1,3}(?:\.\d+)?)\b",
        r"\b(?:be|is|was|reach|reaches|temperature\s+be|temp\s+be)\s+(\d{1,3}(?:\.\d+)?)\s*(?:-|to|--)\s*(\d{1,3}(?:\.\d+)?)\s*(?:degrees?|deg|°|Â°)?",
        r"\b(\d{1,3}(?:\.\d+)?)\s*(?:-|to|--)\s*(\d{1,3}(?:\.\d+)?)\s*(?:degrees?|deg|°|Â°)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        low = float(match.group(1))
        high = float(match.group(2))
        if low > high:
            low, high = high, low
        if 20 <= low <= 130 and 20 <= high <= 130 and high - low <= 20:
            return low, high
    return None


def _detect_text_threshold(text: str) -> tuple[float, str] | None:
    patterns = [
        (r"(?:at\s+least|at\s+or\s+above|greater\s+than\s+or\s+equal\s+to|no\s+less\s+than|>=)\s*(\d{1,3}(?:\.\d+)?)", "gte"),
        (r"(?:above|over|greater\s+than|more\s+than|>)\s*(\d{1,3}(?:\.\d+)?)", "gt"),
        (r"(\d{1,3}(?:\.\d+)?)\s*(?:degrees?|deg|°|Â°)?\s*(?:f|fahrenheit)?\s*(?:or\s+higher|or\s+above|and\s+above)", "gte"),
        (r"(?:at\s+most|at\s+or\s+below|less\s+than\s+or\s+equal\s+to|no\s+more\s+than|<=)\s*(\d{1,3}(?:\.\d+)?)", "lte"),
        (r"(?:below|under|less\s+than|fewer\s+than|<)\s*(\d{1,3}(?:\.\d+)?)", "lt"),
        (r"(\d{1,3}(?:\.\d+)?)\s*(?:degrees?|deg|°|Â°)?\s*(?:f|fahrenheit)?\s*(?:or\s+lower|or\s+below|and\s+below)", "lte"),
    ]
    for pattern, comparator in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return float(match.group(1)), comparator
    return None


def _detect_strike_fallback(market: dict) -> tuple[float, str] | None:
    for key in ("floor_strike", "cap_strike"):
        value = market.get(key)
        if value in (None, ""):
            continue
        try:
            strike_type = str(market.get("strike_type") or "").lower()
            comparator = "gt" if strike_type in {"greater", "above", "gt"} else "lt"
            if strike_type in {"gte", "greater_or_equal"}:
                comparator = "gte"
            if strike_type in {"lte", "less_or_equal"}:
                comparator = "lte"
            return float(value), comparator
        except (TypeError, ValueError):
            continue
    return None
