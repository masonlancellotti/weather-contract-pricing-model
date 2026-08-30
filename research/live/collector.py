from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from backtest.replay_builder import ReplayBuilder
from config import Settings, settings
from data.storage import Storage
from data.weather_settlement_loader import WeatherSettlementLoader
from live.orderbook_recorder import LiveOrderbookRecorder
from live.scanner import LiveScanner

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectorResult:
    started_at: datetime
    finished_at: datetime
    cycles: int
    orderbook_snapshots: int
    scanner_rows: int
    settlement_runs: int
    replay_runs: int
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "cycles": self.cycles,
            "orderbook_snapshots": self.orderbook_snapshots,
            "scanner_rows": self.scanner_rows,
            "settlement_runs": self.settlement_runs,
            "replay_runs": self.replay_runs,
            "warnings": self.warnings,
        }


class LiveDataCollector:
    """Long-running, no-trading collector for live weather-market research data.

    This intentionally does not place orders. It records current full orderbooks,
    periodically runs the scanner so signals/model outputs are logged, and tries
    to refresh exact settlement labels plus replay snapshots for recently
    resolved markets.
    """

    def __init__(self, storage: Storage | None = None, cfg: Settings = settings):
        self.storage = storage or Storage()
        self.cfg = cfg
        self.recorder = LiveOrderbookRecorder(storage=self.storage, cfg=cfg)
        self.scanner = LiveScanner(storage=self.storage)
        self.settlements = WeatherSettlementLoader(storage=self.storage)
        self.replay = ReplayBuilder(storage=self.storage)

    def run(
        self,
        *,
        duration_hours: float | None = 72.0,
        interval_seconds: int | None = None,
        max_markets: int | None = None,
        scan_interval_minutes: int = 5,
        maintenance_interval_minutes: int = 60,
        settlement_lookback_days: int = 7,
        weather_only: bool = True,
    ) -> CollectorResult:
        self.storage.init_db()
        started_at = datetime.now(timezone.utc)
        deadline = started_at + timedelta(hours=duration_hours) if duration_hours and duration_hours > 0 else None
        interval_seconds = interval_seconds or self.cfg.orderbook_record_interval_seconds
        max_markets = max_markets or self.cfg.orderbook_record_max_markets
        next_scan_at = started_at + timedelta(minutes=max(scan_interval_minutes, 1))
        next_maintenance_at = started_at + timedelta(minutes=max(maintenance_interval_minutes, 5))
        cycles = 0
        initial_orderbook_snapshots = _count_orderbook_snapshots(self.storage)
        orderbook_snapshots = 0
        scanner_rows = 0
        settlement_runs = 0
        replay_runs = 0
        warnings: list[str] = []
        recorder_result: dict[str, Any] = {}

        LOGGER.info(
            "collector started duration_hours=%s interval_seconds=%s max_markets=%s live_trading_enabled=%s",
            duration_hours,
            interval_seconds,
            max_markets,
            self.cfg.enable_live_trading,
        )
        if self.cfg.enable_live_trading:
            LOGGER.warning("ENABLE_LIVE_TRADING is true, but collector does not place orders.")

        def record_orderbooks() -> None:
            try:
                recorder_result["result"] = self.recorder.run(
                    weather_only=weather_only,
                    interval_seconds=interval_seconds,
                    duration_hours=duration_hours,
                    max_markets=max_markets,
                    full_depth=self.cfg.orderbook_record_full_depth,
                )
            except Exception as exc:
                recorder_result["error"] = str(exc)
                LOGGER.exception("orderbook recorder thread failed")

        recorder_thread = threading.Thread(target=record_orderbooks, name="orderbook-recorder", daemon=True)
        recorder_thread.start()
        LOGGER.info("orderbook recorder isolated in background thread")

        try:
            while True:
                now = datetime.now(timezone.utc)
                if deadline and now >= deadline:
                    break
                cycles += 1
                if recorder_result.get("error"):
                    warnings.append(f"orderbook recorder thread failed: {recorder_result['error']}")
                    break
                orderbook_snapshots = max(0, _count_orderbook_snapshots(self.storage) - initial_orderbook_snapshots)

                now = datetime.now(timezone.utc)
                if now >= next_scan_at:
                    completed, rows, message = _run_with_timeout(
                        "scanner",
                        60,
                        lambda: self.scanner.scan_once(max_markets=max_markets),
                    )
                    if completed:
                        scanner_rows += len(rows)
                        LOGGER.info("scanner logged rows=%s total_rows=%s", len(rows), scanner_rows)
                    else:
                        LOGGER.warning(message)
                        warnings.append(message)
                    next_scan_at = now + timedelta(minutes=max(scan_interval_minutes, 1))

                now = datetime.now(timezone.utc)
                if now >= next_maintenance_at:
                    start_date, end_date = _recent_date_window(settlement_lookback_days)
                    completed, settlement_result, message = _run_with_timeout(
                        "settlement refresh",
                        180,
                        lambda: self.settlements.build_settlements(start=start_date, end=end_date),
                    )
                    if completed:
                        settlement_runs += 1
                        LOGGER.info("settlement refresh %s", settlement_result.to_dict())
                    else:
                        LOGGER.warning(message)
                        warnings.append(message)
                    completed, replay_result, message = _run_with_timeout(
                        "live-orderbook replay refresh",
                        180,
                        lambda: self.replay.build_from_live_orderbooks(start=start_date, end=end_date),
                    )
                    if completed:
                        replay_runs += 1
                        LOGGER.info("live-orderbook replay refresh %s", replay_result.to_dict())
                    else:
                        LOGGER.warning(message)
                        warnings.append(message)
                    next_maintenance_at = now + timedelta(minutes=max(maintenance_interval_minutes, 5))

                time.sleep(min(max(interval_seconds, 1), 30))
        except KeyboardInterrupt:
            LOGGER.info("collector stopped by user")

        finished_at = datetime.now(timezone.utc)
        if recorder_thread.is_alive() and deadline and finished_at >= deadline:
            recorder_thread.join(timeout=5)
        result = recorder_result.get("result")
        if result:
            warnings.extend(result.warnings)
        orderbook_snapshots = max(0, _count_orderbook_snapshots(self.storage) - initial_orderbook_snapshots)
        LOGGER.info(
            "collector finished cycles=%s snapshots=%s scanner_rows=%s settlement_runs=%s replay_runs=%s",
            cycles,
            orderbook_snapshots,
            scanner_rows,
            settlement_runs,
            replay_runs,
        )
        return CollectorResult(
            started_at=started_at,
            finished_at=finished_at,
            cycles=cycles,
            orderbook_snapshots=orderbook_snapshots,
            scanner_rows=scanner_rows,
            settlement_runs=settlement_runs,
            replay_runs=replay_runs,
            warnings=warnings[-100:],
        )


def _recent_date_window(lookback_days: int) -> tuple[date, date]:
    end_date = date.today()
    start_date = end_date - timedelta(days=max(lookback_days, 1))
    return start_date, end_date


def _count_orderbook_snapshots(storage: Storage) -> int:
    try:
        frame = storage.fetch_sql("SELECT COUNT(*) AS count FROM orderbook_snapshots_live")
        return int(frame.iloc[0]["count"]) if not frame.empty else 0
    except Exception:
        return 0


def _run_with_timeout(name: str, timeout_seconds: int, func: Callable[[], Any]) -> tuple[bool, Any, str | None]:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = func()
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=target, name=f"optional-{name}", daemon=True)
    thread.start()
    thread.join(timeout=max(timeout_seconds, 1))
    if thread.is_alive():
        return False, None, f"{name} timed out after {timeout_seconds}s; orderbook recorder kept running"
    if "error" in box:
        return False, None, f"{name} failed: {box['error']}"
    return True, box.get("value"), None
