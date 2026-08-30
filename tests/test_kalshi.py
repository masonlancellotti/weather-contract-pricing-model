"""
tests/test_kalshi.py — Kalshi adapter unit tests (no live API calls).
"""
import pytest
from decimal import Decimal

from venues.kalshi import _parse_bucket, _ticker_to_city, _map_status
from models import OrderStatus


def test_parse_bucket_range():
    lo, hi = _parse_bucket("68 to 72")
    assert lo == 68.0
    assert hi == 72.0


def test_parse_bucket_gte():
    lo, hi = _parse_bucket("≥ 90")
    assert lo == 90.0
    assert hi is None


def test_parse_bucket_lt():
    lo, hi = _parse_bucket("< 40")
    assert lo is None
    assert hi == 40.0


def test_parse_bucket_dash():
    lo, hi = _parse_bucket("72-76")
    assert lo == 72.0
    assert hi == 76.0


def test_ticker_to_city_ny():
    assert _ticker_to_city("KXHIGHNY-24APR01-T72") == "NYC"


def test_ticker_to_city_chi():
    assert _ticker_to_city("KXHIGHCHI-24APR01-T68") == "CHI"


def test_ticker_to_city_updated_series():
    assert _ticker_to_city("KXHIGHTATL-24APR01-T88") == "ATL"
    assert _ticker_to_city("KXHIGHLAX-24APR01-T75") == "LA"


def test_map_status():
    assert _map_status("resting") == OrderStatus.OPEN
    assert _map_status("executed") == OrderStatus.FILLED
    assert _map_status("canceled") == OrderStatus.CANCELLED
