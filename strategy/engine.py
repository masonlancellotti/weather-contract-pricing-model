"""
strategy/engine.py - Main strategy loop.

Called on each tick. Builds per-city models, applies data-quality gates,
computes signals, then passes surviving signals to execution.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from config import cfg
from models import MarketSnapshot, ModelOutput, Venue
from storage import DB
from strategy.execution import cancel_stale_orders, execute_signal
from strategy.pricing import compute_edge
from strategy.sizing import size_signal
from strategy.risk import is_safe_mode
from utils.time import can_open_new_same_day_positions, city_local_now, settlement_date_for_city
from weather.mapping import CityMeta, active_cities
from weather.model import build_model, snapshots_to_buckets
from weather.providers import (
    get_cdo_daily_highs,
    get_iem_daily_highs,
    get_nws_forecast,
    get_nws_obs,
    get_nws_observed_high_so_far,
)

log = logging.getLogger("strategy.engine")
UTC = timezone.utc


class StrategyEngine:
    def __init__(self, db: DB, kalshi_client=None, poly_client=None):
        self.db = db
        self.kalshi = kalshi_client
        self.poly = poly_client
        self._last_model: dict[str, ModelOutput] = {}

    def tick(self) -> None:
        """Run one full strategy cycle."""
        if is_safe_mode():
            log.warning("Safe mode - skipping strategy tick")
            return

        cancel_stale_orders(self.db, self.kalshi, self.poly)

        for city_meta in active_cities():
            try:
                self._run_city(city_meta)
            except Exception as exc:
                log.exception("Strategy tick failed for %s: %s", city_meta.city_code, exc)

    def _run_city(self, city_meta: CityMeta) -> None:
        settlement_date = settlement_date_for_city(city_meta.city_code)
        snapshots = self.db.get_latest_snapshots(city_meta.city_code, settlement_date)
        if not snapshots:
            log.debug("No market snapshots for %s %s", city_meta.city_code, settlement_date)
            return

        snaps = [_row_to_snapshot(row) for row in snapshots]
        open_snaps = [snap for snap in snaps if snap.is_open]
        if not open_snaps:
            log.debug("No open markets for %s %s", city_meta.city_code, settlement_date)
            return
        if not self._market_data_is_fresh(city_meta.city_code, open_snaps):
            return

        model = self._get_model(city_meta, settlement_date, open_snaps)
        if not model or model.confidence < cfg.min_model_confidence:
            log.info("%s: model confidence too low - skipping", city_meta.city_code)
            return

        balances = self._get_balances()
        allow_new_entries = can_open_new_same_day_positions(city_meta.city_code)
        if not allow_new_entries:
            log.info(
                "%s: outside local entry window at %s - suppressing new same-day positions",
                city_meta.city_code,
                city_local_now(city_meta.city_code).isoformat(),
            )

        for snap in open_snaps:
            if not allow_new_entries:
                continue
            signal = compute_edge(snap, model)
            if signal is None:
                continue

            signal = size_signal(signal, snap, model, self.db)
            if signal is None:
                log.info("Signal suppressed by sizing: %s %s", snap.venue.value, snap.market_id)
                continue

            log.info(
                "Signal: %s %s %s edge=%.3f reason=%s",
                snap.venue.value,
                snap.market_id,
                signal.side.value,
                signal.edge,
                signal.reason,
            )
            balance = balances.get(snap.venue, Decimal("0"))
            execute_signal(signal, balance, self.db, self.kalshi, self.poly)

    def _get_model(
        self,
        city_meta: CityMeta,
        settlement_date: str,
        snapshots: list[MarketSnapshot],
    ) -> Optional[ModelOutput]:
        cached = self._last_model.get(city_meta.city_code)
        if cached and cached.settlement_date == settlement_date:
            age_seconds = (datetime.now(UTC) - cached.ts.astimezone(UTC)).total_seconds()
            if age_seconds < cfg.weather_refresh_seconds:
                return cached

        try:
            local_now = city_local_now(city_meta.city_code)
            current_local_date = local_now.date().isoformat()
            obs = get_nws_obs(city_meta)
            forecast = get_nws_forecast(city_meta, settlement_date)
            observed_high_so_far = None
            if settlement_date == current_local_date:
                observed_high_so_far = get_nws_observed_high_so_far(city_meta, settlement_date)

            hist = get_cdo_daily_highs(city_meta.cdo_station, days_back=60)
            if len(hist) < 14:
                hist = get_iem_daily_highs(city_meta.nws_station, days_back=60)

            buckets = snapshots_to_buckets(snapshots)
            if not buckets:
                log.warning("%s: no buckets derived from snapshots", city_meta.city_code)
                return None

            quality_ok, quality_reason = self._weather_data_ok(
                city_meta,
                settlement_date,
                local_now,
                obs,
                forecast,
                observed_high_so_far,
                hist,
            )
            if not quality_ok:
                log.warning("%s: weather data quality gate failed - %s", city_meta.city_code, quality_reason)
                return None

            model = build_model(
                city_meta,
                forecast,
                obs,
                hist,
                buckets,
                observed_high_so_far=observed_high_so_far,
                current_local_date=current_local_date,
                current_local_time=local_now,
            )
            self._last_model[city_meta.city_code] = model

            if forecast:
                self.db.set_weather(
                    city_meta.city_code,
                    "forecast",
                    {
                        "high_f": forecast.high_f,
                        "low_f": forecast.low_f,
                        "source": forecast.source,
                    },
                )
            if obs:
                self.db.set_weather(
                    city_meta.city_code,
                    "obs",
                    {
                        "temp_f": obs.temp_f,
                        "obs_time": obs.obs_time.isoformat(),
                        "source": obs.source,
                    },
                )
            self.db.set_weather(
                city_meta.city_code,
                "model",
                {
                    "expected_high": model.expected_high,
                    "std_dev": model.std_dev,
                    "confidence": model.confidence,
                    "obs_temp": model.obs_temp,
                    "forecast_temp": model.forecast_temp,
                    "observed_high_so_far": model.observed_high_so_far,
                    "remaining_forecast_high": model.remaining_forecast_high,
                    "local_hour": model.local_hour,
                    "bucket_probs": model.bucket_probs,
                    "ts": model.ts.isoformat(),
                },
            )
            return model
        except Exception as exc:
            log.exception("Model build failed for %s: %s", city_meta.city_code, exc)
            return None

    def _market_data_is_fresh(self, city: str, snapshots: list[MarketSnapshot]) -> bool:
        newest = max((snap.ts for snap in snapshots), default=None)
        if newest is None:
            return False
        age_seconds = (datetime.now(UTC) - newest.astimezone(UTC)).total_seconds()
        if age_seconds > cfg.max_market_snapshot_age_seconds:
            log.warning("%s: market data stale (%ds old) - skipping", city, int(age_seconds))
            return False
        return True

    def _weather_data_ok(
        self,
        city_meta: CityMeta,
        settlement_date: str,
        local_now: datetime,
        obs,
        forecast,
        observed_high_so_far,
        hist: list[dict],
    ) -> tuple[bool, str]:
        if len(hist) < 14:
            return False, f"insufficient_history:{len(hist)}"
        if forecast is None:
            return False, "missing_forecast"

        same_day = settlement_date == local_now.date().isoformat()
        if not same_day:
            return True, "ok"
        if local_now.hour >= cfg.require_obs_by_local_hour and obs is None:
            return False, "missing_same_day_obs"
        if local_now.hour >= cfg.require_observed_high_by_local_hour and observed_high_so_far is None:
            return False, "missing_observed_high"
        return True, "ok"

    def _get_balances(self) -> dict[Venue, Decimal]:
        result: dict[Venue, Decimal] = {}
        if self.kalshi:
            try:
                result[Venue.KALSHI] = self.kalshi.get_balance().available
            except Exception as exc:
                log.warning("Could not fetch Kalshi balance: %s", exc)
                result[Venue.KALSHI] = Decimal("0")
        if self.poly and self.poly.active:
            try:
                result[Venue.POLYMARKET] = self.poly.get_balance().available
            except Exception as exc:
                log.warning("Could not fetch Polymarket balance: %s", exc)
                result[Venue.POLYMARKET] = Decimal("0")
        return result

    def last_model(self, city: str) -> Optional[ModelOutput]:
        return self._last_model.get(city)


def _row_to_snapshot(row) -> MarketSnapshot:
    from decimal import Decimal as D

    ts = datetime.fromisoformat(row["ts"]) if row["ts"] else datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return MarketSnapshot(
        venue=Venue(row["venue"]),
        market_id=row["market_id"],
        city=row["city"],
        bucket_label=row["bucket_label"],
        bucket_min=row["bucket_min"],
        bucket_max=row["bucket_max"],
        settlement_date=row["settlement_date"],
        best_bid=D(str(row["best_bid"])) if row["best_bid"] else None,
        best_ask=D(str(row["best_ask"])) if row["best_ask"] else None,
        last_price=D(str(row["last_price"])) if row["last_price"] else None,
        yes_bid=None,
        yes_ask=None,
        ts=ts,
        is_open=bool(row["is_open"]),
    )
