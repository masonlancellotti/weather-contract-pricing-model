from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from config import PROJECT_ROOT


@dataclass(frozen=True)
class StationMapping:
    city: str
    station_code: str
    timezone: str
    latitude: float | None = None
    longitude: float | None = None
    confidence: float = 0.75
    source: str = "default"
    station_name: str | None = None
    wfo: str | None = None
    notes: str | None = None


DEFAULT_MAPPINGS: dict[str, StationMapping] = {
    "Atlanta": StationMapping("Atlanta", "KATL", "America/New_York", 33.6407, -84.4277, 0.8, station_name="Atlanta Hartsfield-Jackson"),
    "ATL": StationMapping("ATL", "KATL", "America/New_York", 33.6407, -84.4277, 0.8, station_name="Atlanta Hartsfield-Jackson"),
    "New York": StationMapping("New York", "KNYC", "America/New_York", 40.7794, -73.9692, 0.7, "default_central_park", station_name="Central Park", notes="Verify rules; NYC products may use KNYC, KLGA, KJFK, or KEWR."),
    "NYC": StationMapping("NYC", "KNYC", "America/New_York", 40.7794, -73.9692, 0.7, "default_central_park", station_name="Central Park", notes="Verify rules; NYC products may use KNYC, KLGA, KJFK, or KEWR."),
    "Los Angeles": StationMapping("Los Angeles", "KLAX", "America/Los_Angeles", 33.9416, -118.4085, 0.75, station_name="Los Angeles International"),
    "LAX": StationMapping("LAX", "KLAX", "America/Los_Angeles", 33.9416, -118.4085, 0.75, station_name="Los Angeles International"),
    "Chicago": StationMapping("Chicago", "KORD", "America/Chicago", 41.9742, -87.9073, 0.7, station_name="Chicago O'Hare", notes="Verify rules; Chicago may use O'Hare or Midway."),
    "CHI": StationMapping("CHI", "KORD", "America/Chicago", 41.9742, -87.9073, 0.7, station_name="Chicago O'Hare", notes="Verify rules; Chicago may use O'Hare or Midway."),
    "KMDW": StationMapping("Chicago Midway", "KMDW", "America/Chicago", 41.7868, -87.7522, 0.75, station_name="Chicago Midway", notes="Curated station-code mapping for explicit Midway markets and forecast collection."),
    "Miami": StationMapping("Miami", "KMIA", "America/New_York", 25.7959, -80.2870, 0.75, station_name="Miami International"),
    "MIA": StationMapping("MIA", "KMIA", "America/New_York", 25.7959, -80.2870, 0.75, station_name="Miami International"),
    "Dallas": StationMapping("Dallas", "KDFW", "America/Chicago", 32.8998, -97.0403, 0.75, station_name="Dallas/Fort Worth"),
    "DFW": StationMapping("DFW", "KDFW", "America/Chicago", 32.8998, -97.0403, 0.75, station_name="Dallas/Fort Worth"),
    "Philadelphia": StationMapping("Philadelphia", "KPHL", "America/New_York", 39.8744, -75.2424, 0.75, station_name="Philadelphia International"),
    "PHIL": StationMapping("PHIL", "KPHL", "America/New_York", 39.8744, -75.2424, 0.75, station_name="Philadelphia International"),
    "Boston": StationMapping("Boston", "KBOS", "America/New_York", 42.3656, -71.0096, 0.75, station_name="Boston Logan"),
    "BOS": StationMapping("BOS", "KBOS", "America/New_York", 42.3656, -71.0096, 0.75, station_name="Boston Logan"),
    "Washington DC": StationMapping("Washington DC", "KDCA", "America/New_York", 38.8512, -77.0402, 0.75, station_name="Washington Reagan National"),
    "Houston": StationMapping("Houston", "KIAH", "America/Chicago", 29.9902, -95.3368, 0.75, station_name="Houston Intercontinental"),
    "Phoenix": StationMapping("Phoenix", "KPHX", "America/Phoenix", 33.4343, -112.0116, 0.75, station_name="Phoenix Sky Harbor"),
    "Seattle": StationMapping("Seattle", "KSEA", "America/Los_Angeles", 47.4502, -122.3088, 0.75, station_name="Seattle-Tacoma"),
    "Denver": StationMapping("Denver", "KDEN", "America/Denver", 39.8561, -104.6737, 0.75, station_name="Denver International"),
    "DEN": StationMapping("DEN", "KDEN", "America/Denver", 39.8561, -104.6737, 0.75, station_name="Denver International"),
    "Las Vegas": StationMapping("Las Vegas", "KLAS", "America/Los_Angeles", 36.0840, -115.1537, 0.75, station_name="Harry Reid International"),
    "San Francisco": StationMapping("San Francisco", "KSFO", "America/Los_Angeles", 37.6213, -122.3790, 0.7, station_name="San Francisco International"),
    "Austin": StationMapping("Austin", "KAUS", "America/Chicago", 30.1945, -97.6699, 0.75, station_name="Austin-Bergstrom", notes="Verify exact Kalshi source; prior AUS labels needed scrutiny."),
    "AUS": StationMapping("AUS", "KAUS", "America/Chicago", 30.1945, -97.6699, 0.75, station_name="Austin-Bergstrom", notes="Verify exact Kalshi source; prior AUS labels needed scrutiny."),
}


class StationMapper:
    def __init__(self, override_path: Path | None = None):
        self.override_path = override_path or _default_override_path()
        self.mappings = dict(DEFAULT_MAPPINGS)
        self._load_overrides()

    def resolve(self, city: str | None, station_from_rules: str | None = None) -> StationMapping | None:
        if station_from_rules:
            inferred_city = city or station_from_rules
            base = self.mappings.get(inferred_city) if city else None
            return StationMapping(
                city=inferred_city,
                station_code=station_from_rules.upper(),
                timezone=base.timezone if base else "America/New_York",
                latitude=base.latitude if base else None,
                longitude=base.longitude if base else None,
                confidence=0.95,
                source="rules",
                station_name=base.station_name if base else None,
                wfo=base.wfo if base else None,
                notes="Station code was explicit in rules/title; rules beat default mapping.",
            )
        if not city:
            return None
        return self.mappings.get(city) or self.mappings.get(city.upper())

    def resolve_station_code(self, station_code: str) -> StationMapping:
        normalized = station_code.upper()
        for mapping in self.mappings.values():
            if mapping.station_code.upper() == normalized:
                return mapping
        return StationMapping(city=normalized, station_code=normalized, timezone="America/New_York", confidence=0.5, source="station_code_only", notes="Station code supplied without curated timezone/lat/lon metadata.")

    def _load_overrides(self) -> None:
        if not self.override_path.exists():
            return
        raw = self.override_path.read_text(encoding="utf-8")
        if self.override_path.suffix.lower() in {".yaml", ".yml"} and yaml:
            data = yaml.safe_load(raw) or {}
        else:
            data = json.loads(raw)
        for city, payload in data.items():
            station_code = payload.get("station_code") or payload.get("default_station")
            if not station_code:
                continue
            base = self.mappings.get(city) or self.mappings.get(str(city).upper())
            self.mappings[city] = StationMapping(
                city=city,
                station_code=str(station_code).upper(),
                timezone=payload.get("timezone") or (base.timezone if base else "America/New_York"),
                latitude=payload.get("latitude", base.latitude if base else None),
                longitude=payload.get("longitude", base.longitude if base else None),
                confidence=float(payload.get("confidence", 0.9)),
                source="override",
                station_name=payload.get("station_name", base.station_name if base else None),
                wfo=payload.get("wfo", base.wfo if base else None),
                notes=payload.get("notes"),
            )


def _default_override_path() -> Path:
    yaml_path = PROJECT_ROOT / "config" / "station_overrides.yaml"
    if yaml_path.exists():
        return yaml_path
    return PROJECT_ROOT / "config" / "station_overrides.json"
