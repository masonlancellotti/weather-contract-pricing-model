"""
weather/mapping.py - Explicit city to settlement-station mappings.

Kalshi daily-high contracts settle on the NWS Daily Climate Report for a
specific station. The NWS station, NOAA CDO station, and forecast coordinates
must all line up with that settlement source.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class CityMeta:
    city_code: str
    display_name: str
    kalshi_series: str
    nws_station: str
    cdo_station: str
    lat: float
    lon: float
    timezone: str
    kalshi_city_code: str


CITIES: list[CityMeta] = [
    CityMeta("NYC", "New York City", "KXHIGHNY", "KNYC", "GHCND:USW00094728", 40.7789, -73.9692, "America/New_York", "NY"),
    CityMeta("CHI", "Chicago", "KXHIGHCHI", "KMDW", "GHCND:USW00014819", 41.7860, -87.7520, "America/Chicago", "CHI"),
    CityMeta("ATL", "Atlanta", "KXHIGHTATL", "KATL", "GHCND:USW00013874", 33.6407, -84.4277, "America/New_York", "TATL"),
    CityMeta("BOS", "Boston", "KXHIGHTBOS", "KBOS", "GHCND:USW00014739", 42.3606, -71.0097, "America/New_York", "TBOS"),
    CityMeta("DAL", "Dallas", "KXHIGHTDAL", "KDFW", "GHCND:USW00003927", 32.8998, -97.0403, "America/Chicago", "TDAL"),
    CityMeta("HOU", "Houston", "KXHIGHTHOU", "KHOU", "GHCND:USW00012918", 29.6375, -95.2825, "America/Chicago", "THOU"),
    CityMeta("MIA", "Miami", "KXHIGHMIA", "KMIA", "GHCND:USW00012839", 25.7959, -80.2870, "America/New_York", "MIA"),
    CityMeta("AUS", "Austin", "KXHIGHAUS", "KAUS", "GHCND:USW00013904", 30.1944, -97.6700, "America/Chicago", "AUS"),
    CityMeta("DC", "Washington, DC", "KXHIGHTDC", "KDCA", "GHCND:USW00013743", 38.8521, -77.0377, "America/New_York", "TDC"),
    CityMeta("PHX", "Phoenix", "KXHIGHTPHX", "KPHX", "GHCND:USW00023183", 33.4373, -112.0078, "America/Phoenix", "TPHX"),
    CityMeta("DEN", "Denver", "KXHIGHDEN", "KDEN", "GHCND:USW00003017", 39.8561, -104.6737, "America/Denver", "DEN"),
    CityMeta("PHIL", "Philadelphia", "KXHIGHPHIL", "KPHL", "GHCND:USW00013739", 39.8729, -75.2437, "America/New_York", "PHIL"),
    CityMeta("LV", "Las Vegas", "KXHIGHTLV", "KLAS", "GHCND:USW00023169", 36.0801, -115.1522, "America/Los_Angeles", "TLV"),
    CityMeta("MIN", "Minneapolis", "KXHIGHTMIN", "KMSP", "GHCND:USW00014922", 44.8848, -93.2223, "America/Chicago", "TMIN"),
    CityMeta("OKC", "Oklahoma City", "KXHIGHTOKC", "KOKC", "GHCND:USW00013967", 35.3931, -97.6007, "America/Chicago", "TOKC"),
    CityMeta("SEA", "Seattle", "KXHIGHTSEA", "KSEA", "GHCND:USW00024233", 47.4502, -122.3088, "America/Los_Angeles", "TSEA"),
    CityMeta("SF", "San Francisco", "KXHIGHTSFO", "KSFO", "GHCND:USW00023234", 37.6213, -122.3790, "America/Los_Angeles", "TSFO"),
    CityMeta("LA", "Los Angeles", "KXHIGHLAX", "KLAX", "GHCND:USW00023174", 33.9425, -118.4081, "America/Los_Angeles", "LAX"),
    CityMeta("SATX", "San Antonio", "KXHIGHTSATX", "KSAT", "GHCND:USW00012921", 29.5337, -98.4698, "America/Chicago", "TSATX"),
]

CITY_META: dict[str, CityMeta] = {city.city_code: city for city in CITIES}
STATION_TO_CITY: dict[str, str] = {city.nws_station: city.city_code for city in CITIES}
CITY_ALIASES: dict[str, str] = {
    "LAX": "LA",
    "SFO": "SF",
    "LAS": "LV",
    "MSP": "MIN",
    "PHL": "PHIL",
    "SAT": "SATX",
}


def get_city(city_code: str) -> Optional[CityMeta]:
    canonical = CITY_ALIASES.get(city_code.upper(), city_code.upper())
    return CITY_META.get(canonical)


def active_cities() -> list[CityMeta]:
    """Return CityMeta objects for configured active cities."""
    from config import cfg

    result: list[CityMeta] = []
    seen: set[str] = set()
    for code in cfg.active_cities:
        meta = get_city(code)
        if meta and meta.city_code not in seen:
            result.append(meta)
            seen.add(meta.city_code)
    return result
