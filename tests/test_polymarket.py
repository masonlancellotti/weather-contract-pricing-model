"""
tests/test_polymarket.py â€” Polymarket adapter unit tests (no live API calls).
"""

from venues.polymarket import (
    _city_to_search_term,
    _parse_poly_bucket,
    _slug_to_city,
    live_trading_enabled,
)


def test_slug_to_city_nyc():
    assert _slug_to_city("will-new-york-city-high-be-above-72") == "NYC"


def test_slug_to_city_chi():
    assert _slug_to_city("chicago-temperature-april-21") == "CHI"


def test_city_to_search_term():
    assert _city_to_search_term("NYC") == "New York"
    assert _city_to_search_term("CHI") == "Chicago"


def test_parse_poly_bucket_above():
    lo, hi = _parse_poly_bucket("Will the high be above 72°F?")
    assert lo == 72.0
    assert hi is None


def test_parse_poly_bucket_range():
    lo, hi = _parse_poly_bucket("Will high temp be 68-72°F?")
    assert lo == 68.0
    assert hi == 72.0


def test_parse_poly_bucket_below():
    lo, hi = _parse_poly_bucket("Will high temp be below 50°F?")
    assert lo is None
    assert hi == 50.0


def test_polymarket_live_trading_disabled_by_default():
    assert live_trading_enabled() is False
