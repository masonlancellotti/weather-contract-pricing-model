from __future__ import annotations

import logging
import html
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import requests

from data.nws_climate_report_parser import ParsedClimateReport, parse_cli_report
from data.storage import Storage

LOGGER = logging.getLogger(__name__)


CLIMATE_PRODUCT_MAP: dict[str, tuple[str, str]] = {
    "KATL": ("ATL", "FFC"),
    "KNYC": ("NYC", "OKX"),
    "KLGA": ("LGA", "OKX"),
    "KJFK": ("JFK", "OKX"),
    "KEWR": ("EWR", "OKX"),
    "KORD": ("ORD", "LOT"),
    "KMDW": ("MDW", "LOT"),
    "KLAX": ("LAX", "LOX"),
    "KMIA": ("MIA", "MFL"),
    "KDFW": ("DFW", "FWD"),
    "KPHL": ("PHL", "PHI"),
    "KBOS": ("BOS", "BOX"),
    "KDCA": ("DCA", "LWX"),
    "KIAH": ("IAH", "HGX"),
    "KPHX": ("PHX", "PSR"),
    "KSEA": ("SEA", "SEW"),
    "KDEN": ("DEN", "BOU"),
    "KLAS": ("LAS", "VEF"),
    "KSFO": ("SFO", "MTR"),
    "KAUS": ("AUS", "EWX"),
    "KSAT": ("SAT", "EWX"),
    "KOKC": ("OKC", "OUN"),
    "KMSP": ("MSP", "MPX"),
    "KMSY": ("MSY", "LIX"),
}


@dataclass(frozen=True)
class ClimateReportResult:
    station_code: str
    local_date: date
    report_product_id: str | None
    office: str | None
    report_url: str | None
    issued_at: datetime | None
    raw_text: str | None
    parsed: ParsedClimateReport | None
    warnings: list[str]

    @property
    def found_exact_date(self) -> bool:
        return bool(self.parsed and self.parsed.report_date == self.local_date and self.parsed.confidence >= 0.85)

    def to_storage_row(self) -> dict:
        parsed = self.parsed
        warnings = list(self.warnings)
        if parsed:
            warnings.extend(parsed.warnings)
        return {
            "station_code": self.station_code,
            "local_date": self.local_date.isoformat(),
            "report_product_id": self.report_product_id,
            "office": self.office,
            "report_url": self.report_url,
            "issued_at": self.issued_at,
            "raw_text": self.raw_text,
            "parsed_high_temp": parsed.high_temp if parsed else None,
            "parsed_low_temp": parsed.low_temp if parsed else None,
            "parsed_precip": parsed.precip if parsed else None,
            "parsed_snowfall": parsed.snowfall if parsed else None,
            "parser_confidence": parsed.confidence if parsed else 0.0,
            "warnings": "; ".join(sorted(set(warnings))),
        }


class NWSClimateReportClient:
    """Fetch NWS Daily Climate Report (CLI) products when available."""

    def __init__(self, storage: Storage | None = None, session: requests.Session | None = None):
        self.storage = storage or Storage()
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-weather-edge/0.1 research bot"})

    def fetch_report(self, station_code: str, local_date: date, persist: bool = True) -> ClimateReportResult:
        station_code = station_code.upper()
        issuedby, office = station_to_cli_product(station_code)
        warnings: list[str] = []

        api_result = self._fetch_from_api_products(station_code, local_date, issuedby, warnings)
        if api_result and api_result.found_exact_date:
            if persist:
                self.storage.upsert_nws_daily_climate_report(api_result.to_storage_row())
            return api_result

        web_result = self._fetch_from_forecast_product(station_code, local_date, issuedby, office, warnings)
        result = web_result or api_result or ClimateReportResult(station_code, local_date, None, office, None, None, None, None, warnings)
        if persist:
            self.storage.upsert_nws_daily_climate_report(result.to_storage_row())
        return result

    def _fetch_from_api_products(self, station_code: str, local_date: date, issuedby: str, warnings: list[str]) -> ClimateReportResult | None:
        url = f"https://api.weather.gov/products/types/CLI/locations/{issuedby}"
        try:
            response = self.session.get(url, timeout=20)
            if response.status_code >= 400:
                warnings.append(f"NWS API products lookup failed {response.status_code} for CLI{issuedby}")
                return None
            products = response.json().get("@graph", []) or response.json().get("features", [])
        except requests.RequestException as exc:
            warnings.append(f"NWS API products lookup error for CLI{issuedby}: {exc}")
            return None
        best: ClimateReportResult | None = None
        for product in products[:50]:
            product_id = product.get("id") or product.get("@id", "").rstrip("/").split("/")[-1]
            if not product_id:
                continue
            try:
                detail = self.session.get(f"https://api.weather.gov/products/{product_id}", timeout=20)
                if detail.status_code >= 400:
                    continue
                payload = detail.json()
                raw_text = payload.get("productText") or payload.get("text")
                if not raw_text:
                    continue
                parsed = parse_cli_report(raw_text)
                issued_at = _parse_time(payload.get("issuanceTime") or payload.get("issued"))
                result = ClimateReportResult(
                    station_code,
                    local_date,
                    product_id,
                    payload.get("issuingOffice") or payload.get("wmoCollectiveId"),
                    f"https://api.weather.gov/products/{product_id}",
                    issued_at,
                    raw_text,
                    parsed,
                    warnings.copy(),
                )
                if parsed.report_date == local_date:
                    return result
                if best is None:
                    best = result
            except requests.RequestException:
                continue
        if best:
            best.warnings.append(f"latest CLI{issuedby} product did not match requested date {local_date}")
        return best

    def _fetch_from_forecast_product(self, station_code: str, local_date: date, issuedby: str, office: str, warnings: list[str]) -> ClimateReportResult | None:
        url = "https://forecast.weather.gov/product.php"
        params = {"site": "NWS", "issuedby": issuedby, "product": "CLI", "format": "txt", "version": 1, "glossary": 0}
        try:
            response = self.session.get(url, params=params, timeout=20)
            if response.status_code >= 400:
                warnings.append(f"forecast.weather.gov CLI fetch failed {response.status_code} for CLI{issuedby}")
                return None
            raw_text = _extract_product_text(response.text)
        except requests.RequestException as exc:
            warnings.append(f"forecast.weather.gov CLI fetch error for CLI{issuedby}: {exc}")
            return None
        parsed = parse_cli_report(raw_text)
        if parsed.report_date != local_date:
            warnings = [*warnings, f"fetched current CLI{issuedby} report date {parsed.report_date}, not requested {local_date}"]
        return ClimateReportResult(station_code, local_date, f"CLI{issuedby}", office, response.url, None, raw_text, parsed, warnings)


def station_to_cli_product(station_code: str) -> tuple[str, str]:
    station_code = station_code.upper()
    if station_code in CLIMATE_PRODUCT_MAP:
        return CLIMATE_PRODUCT_MAP[station_code]
    suffix = station_code.removeprefix("K")
    return suffix, "UNKNOWN"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None


def _extract_product_text(raw: str) -> str:
    match = re.search(r'<pre[^>]*class="[^"]*glossaryProduct[^"]*"[^>]*>(.*?)</pre>', raw, re.IGNORECASE | re.DOTALL)
    if not match:
        match = re.search(r"<pre[^>]*>(.*?)</pre>", raw, re.IGNORECASE | re.DOTALL)
    if match:
        return html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
    return raw
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
