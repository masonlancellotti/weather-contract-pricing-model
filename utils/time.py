"""
utils/time.py — Time helpers. All internal time in UTC. Explicit conversions only.
"""
from __future__ import annotations
from datetime import datetime, timezone, date, timedelta

import pytz

from config import cfg


UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_ms() -> int:
    """Milliseconds since epoch — required by Kalshi signing."""
    return int(utcnow().timestamp() * 1000)


def today_local(tz_name: str) -> date:
    """Today's date in the given timezone."""
    return datetime.now(pytz.timezone(tz_name)).date()


def city_local_now(city: str, now_utc: datetime | None = None) -> datetime:
    """Current local time for the configured city."""
    from weather.mapping import CITY_META

    meta = CITY_META.get(city)
    tz_name = meta.timezone if meta else "America/New_York"
    tz = pytz.timezone(tz_name)
    base = now_utc.astimezone(UTC) if now_utc else datetime.now(UTC)
    return base.astimezone(tz)


def hours_until_entry_cutoff(city: str, now_utc: datetime | None = None) -> float:
    """Hours until the configured local entry cutoff for a city."""
    local_now = city_local_now(city, now_utc)
    cutoff = local_now.replace(
        hour=cfg.entry_cutoff_local_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    return max(0.0, (cutoff - local_now).total_seconds() / 3600)


def hours_until_entry_start(city: str, now_utc: datetime | None = None) -> float:
    """Hours until the configured local entry window opens for a city."""
    local_now = city_local_now(city, now_utc)
    start = local_now.replace(
        hour=cfg.entry_start_local_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    return max(0.0, (start - local_now).total_seconds() / 3600)


def can_open_new_same_day_positions(city: str, now_utc: datetime | None = None) -> bool:
    """Whether new same-day positions may be opened for the city."""
    local_now = city_local_now(city, now_utc)
    start = local_now.replace(
        hour=cfg.entry_start_local_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    cutoff = local_now.replace(
        hour=cfg.entry_cutoff_local_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    return start <= local_now < cutoff


def settlement_date_for_city(city: str) -> str:
    """
    Return the settlement date string (YYYY-MM-DD) for the next active
    Kalshi daily-high contract in the given city's local timezone.
    Settlement is for the calendar day that is currently in progress or next.
    """
    from weather.mapping import CITY_META
    meta = CITY_META.get(city)
    tz_name = meta.timezone if meta else "America/New_York"
    local_now = datetime.now(pytz.timezone(tz_name))
    # If it's before 6am local, current day hasn't had much data yet —
    # still reference today (settlement covers midnight-to-midnight local).
    return local_now.date().isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
