from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from backtest.execution import NormalizedOrderBook
from config import Settings, settings
from data.kalshi_client import KalshiClient
from data.kalshi_market_loader import KalshiMarketLoader
from data.storage import Storage

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderbookRecordResult:
    started_at: datetime
    finished_at: datetime
    cycles: int
    snapshots: int
    markets_seen: int
    last_snapshot_at: datetime | None
    stopped_by_user: bool
    warnings: list[str]

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "cycles": self.cycles,
            "snapshots": self.snapshots,
            "markets_seen": self.markets_seen,
            "last_snapshot_at": self.last_snapshot_at.isoformat() if self.last_snapshot_at else None,
            "stopped_by_user": self.stopped_by_user,
            "warnings": self.warnings,
        }


class LiveOrderbookRecorder:
    """Record current Kalshi full orderbooks for future passive replay."""

    def __init__(self, client: KalshiClient | None = None, storage: Storage | None = None, cfg: Settings = settings):
        self.client = client or KalshiClient()
        self.storage = storage or Storage()
        self.cfg = cfg
        self.loader = KalshiMarketLoader(client=self.client, storage=self.storage)
        self.closed_or_failed: set[str] = set()
        self._cached_tickers: list[str] = []
        self._cached_tickers_at: datetime | None = None
        self._next_market_refresh_at: datetime | None = None
        self.last_successful_market_refresh: datetime | None = None

    def run(
        self,
        market_ticker: str | None = None,
        weather_only: bool = True,
        interval_seconds: int | None = None,
        duration_minutes: int | None = None,
        duration_hours: float | None = None,
        max_markets: int | None = None,
        full_depth: bool | None = None,
        verbose_orderbooks: bool = False,
        once: bool = False,
    ) -> OrderbookRecordResult:
        interval_seconds = interval_seconds or self.cfg.orderbook_record_interval_seconds
        max_markets = max_markets or self.cfg.orderbook_record_max_markets
        full_depth = self.cfg.orderbook_record_full_depth if full_depth is None else full_depth
        started_at = datetime.now(timezone.utc)
        if duration_hours is not None and duration_hours > 0:
            deadline = started_at + timedelta(hours=duration_hours)
        elif duration_minutes is not None and duration_minutes > 0:
            deadline = started_at + timedelta(minutes=duration_minutes)
        else:
            deadline = None
        snapshots = 0
        cycles = 0
        markets_seen: set[str] = set()
        warnings: list[str] = []
        last_snapshot_at: datetime | None = None
        last_heartbeat_at: datetime | None = None
        stopped_by_user = False
        self._update_state(
            started_at=started_at,
            last_heartbeat_at=None,
            last_snapshot_at=None,
            cycles=0,
            snapshots=snapshots,
            markets_tracked=0,
            current_task="starting",
            status="STARTING",
            error_message=None,
        )
        LOGGER.info("pure orderbook recorder started interval_seconds=%s duration_hours=%s max_markets=%s", interval_seconds, duration_hours, max_markets)
        while True:
            try:
                cycles += 1
                self._update_state(
                    started_at=started_at,
                    last_heartbeat_at=last_heartbeat_at,
                    last_snapshot_at=last_snapshot_at,
                    cycles=cycles,
                    snapshots=snapshots,
                    markets_tracked=len(markets_seen),
                    current_task="recording_orderbooks",
                    status="RECORDING",
                    error_message=None,
                )
                if market_ticker:
                    tickers = [market_ticker]
                else:
                    self._update_state(
                        started_at=started_at,
                        last_heartbeat_at=last_heartbeat_at,
                        last_snapshot_at=last_snapshot_at,
                        cycles=cycles,
                        snapshots=snapshots,
                        markets_tracked=len(markets_seen) or len(self._cached_tickers),
                        current_task="market_refresh",
                        status="MARKET_REFRESH",
                        error_message=None,
                    )
                    tickers = self._active_weather_tickers(weather_only=weather_only, max_markets=max_markets)
                    self._update_state(
                        started_at=started_at,
                        last_heartbeat_at=last_heartbeat_at,
                        last_snapshot_at=last_snapshot_at,
                        cycles=cycles,
                        snapshots=snapshots,
                        markets_tracked=len(tickers),
                        current_task="recording_orderbooks",
                        status="RECORDING",
                        error_message=None,
                    )
                for ticker in tickers:
                    if ticker in self.closed_or_failed:
                        continue
                    markets_seen.add(ticker)
                    row = self._record_one(ticker, full_depth=full_depth, interval_seconds=interval_seconds)
                    if row:
                        snapshots += 1
                        last_snapshot_at = row["ts"]
                        if verbose_orderbooks or self.cfg.collector_log_every_orderbook:
                            LOGGER.info(
                                "orderbook %s %s bid=%s ask=%s spread=%s depth_bid=%s depth_ask=%s",
                                ticker,
                                row["ts"],
                                row["yes_best_bid"],
                                row["yes_best_ask"],
                                row["spread_cents"],
                                row["depth_yes_bid_1"],
                                row["depth_yes_ask_1"],
                            )
                    if self.cfg.kalshi_orderbook_sleep_between_requests_ms > 0:
                        time.sleep(self.cfg.kalshi_orderbook_sleep_between_requests_ms / 1000.0)
            except KeyboardInterrupt:
                stopped_by_user = True
                break
            except Exception as exc:
                message = f"orderbook recorder cycle failed: {exc}"
                LOGGER.warning(message)
                warnings.append(message)
                self._update_state(
                    started_at=started_at,
                    last_heartbeat_at=last_heartbeat_at,
                    last_snapshot_at=last_snapshot_at,
                    cycles=cycles,
                    snapshots=snapshots,
                    markets_tracked=len(markets_seen),
                    current_task="recording_orderbooks",
                    status="DEGRADED_API_ERRORS",
                    error_message=message,
                )
            now = datetime.now(timezone.utc)
            if _heartbeat_due(now, last_heartbeat_at, interval_seconds):
                last_heartbeat_at = now
                self._heartbeat(
                    started_at=started_at,
                    heartbeat_at=now,
                    cycles=cycles,
                    snapshots=snapshots,
                    markets_tracked=len(markets_seen) or len(self._cached_tickers),
                    last_snapshot_at=last_snapshot_at,
                    interval_seconds=interval_seconds,
                    current_task="recording_orderbooks",
                )
            if once:
                break
            if deadline and datetime.now(timezone.utc) >= deadline:
                break
            self._update_state(
                started_at=started_at,
                last_heartbeat_at=last_heartbeat_at,
                last_snapshot_at=last_snapshot_at,
                cycles=cycles,
                snapshots=snapshots,
                markets_tracked=len(markets_seen) or len(self._cached_tickers),
                current_task="sleeping",
                status="SLEEPING",
                error_message=None,
            )
            try:
                time.sleep(max(interval_seconds, 1))
            except KeyboardInterrupt:
                stopped_by_user = True
                break
        finished_at = datetime.now(timezone.utc)
        healthy_recently = last_snapshot_at is not None and (finished_at - last_snapshot_at).total_seconds() <= 600
        status = "STOPPED" if stopped_by_user else "STOPPED"
        self._update_state(
            started_at=started_at,
            last_heartbeat_at=last_heartbeat_at,
            last_snapshot_at=last_snapshot_at,
            cycles=cycles,
            snapshots=snapshots,
            markets_tracked=len(markets_seen) or len(self._cached_tickers),
            current_task="stopped",
            status=status,
            error_message=None,
        )
        LOGGER.info(
            "recorder stopped_by_user=%s runtime_sec=%.1f cycles=%s snapshots_this_run=%s last_snapshot=%s healthy_last_10m=%s",
            stopped_by_user,
            (finished_at - started_at).total_seconds(),
            cycles,
            snapshots,
            last_snapshot_at,
            healthy_recently,
        )
        print(
            "RECORDER SUMMARY "
            f"stopped_by_user={stopped_by_user} runtime_sec={(finished_at - started_at).total_seconds():.1f} "
            f"cycles={cycles} snapshots_this_run={snapshots} last_snapshot={last_snapshot_at} "
            f"healthy_last_10m={healthy_recently}"
        )
        return OrderbookRecordResult(
            started_at=started_at,
            finished_at=finished_at,
            cycles=cycles,
            snapshots=snapshots,
            markets_seen=len(markets_seen),
            last_snapshot_at=last_snapshot_at,
            stopped_by_user=stopped_by_user,
            warnings=warnings[:50],
        )

    def _active_weather_tickers(self, weather_only: bool, max_markets: int) -> list[str]:
        now = datetime.now(timezone.utc)
        if self._next_market_refresh_at and now < self._next_market_refresh_at and self._cached_tickers:
            LOGGER.info("using cached market list age=%.1fs failure_backoff_active=true", _age_seconds(now, self._cached_tickers_at))
            return self._cached_tickers[:max_markets]
        if self._cached_tickers and self._cached_tickers_at and (now - self._cached_tickers_at).total_seconds() < self.cfg.kalshi_markets_refresh_seconds:
            LOGGER.info("using cached market list age=%.1fs", _age_seconds(now, self._cached_tickers_at))
            return self._cached_tickers[:max_markets]
        try:
            if not weather_only:
                markets = list(self.client.iter_markets(status="open", limit=1000, max_pages=1))
            else:
                markets = self.loader.load_active_weather_markets(persist=False, max_pages=1, max_series=max(1, min(max_markets, 25)))
            self._cached_tickers = [str(market.get("ticker")) for market in markets[:max_markets] if market.get("ticker")]
            self._cached_tickers_at = now
            self.last_successful_market_refresh = now
            self._next_market_refresh_at = now + timedelta(seconds=self.cfg.kalshi_markets_refresh_seconds)
        except Exception as exc:
            LOGGER.warning("active market refresh failed; continuing with cached markets if available: %s", exc)
            self._next_market_refresh_at = now + timedelta(seconds=self.cfg.kalshi_markets_refresh_failure_backoff_seconds)
        return self._cached_tickers[:max_markets]

    def _record_one(self, ticker: str, full_depth: bool, interval_seconds: int) -> dict | None:
        depth = 100 if full_depth else 10
        try:
            raw = self.client.get_orderbook(ticker, depth=depth)
        except Exception as exc:
            LOGGER.warning("orderbook fetch failed for %s: %s", ticker, exc)
            self.closed_or_failed.add(ticker)
            return None
        ts = _bucket_time(datetime.now(timezone.utc), interval_seconds)
        book = NormalizedOrderBook.from_kalshi(ticker, raw)
        row = normalize_live_orderbook_snapshot(ticker, ts, book, raw)
        self.storage.upsert_live_orderbook_snapshot(row)
        return row

    def _heartbeat(
        self,
        *,
        started_at: datetime,
        heartbeat_at: datetime,
        cycles: int,
        snapshots: int,
        markets_tracked: int,
        last_snapshot_at: datetime | None,
        interval_seconds: int,
        current_task: str,
    ) -> None:
        last_age = (heartbeat_at - last_snapshot_at).total_seconds() if last_snapshot_at else None
        db_count = _db_snapshot_count(self.storage)
        rate_limits = int(self.client.stats.get("total_429s", 0))
        market_cache = "active"
        if self._cached_tickers_at and heartbeat_at < (self._next_market_refresh_at or heartbeat_at):
            market_cache = f"cached age={_age_seconds(heartbeat_at, self._cached_tickers_at):.0f}s"
        line = (
            f"HEARTBEAT recorder alive local_time={heartbeat_at.astimezone().isoformat(timespec='seconds')} "
            f"cycles={cycles} snapshots_this_run={snapshots} db_snapshots={db_count} markets={markets_tracked} "
            f"last_snapshot_age_sec={last_age if last_age is not None else 'none'} rate_limits={rate_limits} "
            f"current_task={current_task} market_refresh={market_cache}"
        )
        LOGGER.info(line)
        print(line, flush=True)
        status = "RECORDING"
        if last_age is None or last_age > max(interval_seconds * 2, 1):
            status = "DEGRADED_API_ERRORS"
            LOGGER.warning("No snapshot written in more than two intervals; last_snapshot_age_sec=%s", last_age)
        if last_age is None or last_age > 600:
            status = "ERROR"
            LOGGER.error("No snapshot written in more than 10 minutes; recorder will keep trying.")
        self._update_state(
            started_at=started_at,
            last_heartbeat_at=heartbeat_at,
            last_snapshot_at=last_snapshot_at,
            cycles=cycles,
            snapshots=snapshots,
            markets_tracked=markets_tracked,
            current_task=current_task,
            status=status,
            error_message=None,
        )

    def _update_state(
        self,
        *,
        started_at: datetime,
        last_heartbeat_at: datetime | None,
        last_snapshot_at: datetime | None,
        cycles: int,
        snapshots: int,
        markets_tracked: int,
        current_task: str,
        status: str,
        error_message: str | None,
    ) -> None:
        try:
            self.storage.upsert_collector_state(
                {
                    "collector_name": "orderbook_recorder",
                    "started_at": started_at,
                    "last_heartbeat_at": last_heartbeat_at,
                    "last_snapshot_at": last_snapshot_at,
                    "cycles_completed": cycles,
                    "snapshots_this_run": snapshots,
                    "markets_tracked": markets_tracked,
                    "current_task": current_task,
                    "status": status,
                    "error_message": error_message,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
        except Exception as exc:
            LOGGER.debug("collector_state update failed: %s", exc)


def normalize_live_orderbook_snapshot(ticker: str, ts: datetime, book: NormalizedOrderBook, raw: dict) -> dict:
    yes_bids = [{"price_cents": level.price_cents, "size": level.size} for level in book.yes_bids]
    no_bids = [{"price_cents": level.price_cents, "size": level.size} for level in book.no_bids]
    yes_best_bid = book.yes_bid
    yes_best_ask = book.yes_ask
    no_best_bid = book.no_bid
    no_best_ask = book.no_ask
    return {
        "market_ticker": ticker,
        "ts": ts,
        "yes_bids_json": json.dumps(yes_bids),
        "no_bids_json": json.dumps(no_bids),
        "yes_best_bid": yes_best_bid,
        "yes_best_ask": yes_best_ask,
        "no_best_bid": no_best_bid,
        "no_best_ask": no_best_ask,
        "spread_cents": yes_best_ask - yes_best_bid if yes_best_bid is not None and yes_best_ask is not None else None,
        "mid_cents": (yes_best_bid + yes_best_ask) / 2 if yes_best_bid is not None and yes_best_ask is not None else None,
        "depth_yes_bid_1": book.depth_at_best_bid,
        "depth_yes_ask_1": book.depth_at_best_ask,
        "depth_no_bid_1": book.depth_at_best_ask,
        "depth_no_ask_1": book.depth_at_best_bid,
        "total_yes_bid_depth": sum(level.size for level in book.yes_bids),
        "total_no_bid_depth": sum(level.size for level in book.no_bids),
        "raw_json": json.dumps(raw, default=str),
        "source": "kalshi_current_orderbook",
    }


def _bucket_time(timestamp: datetime, interval_seconds: int) -> datetime:
    epoch = int(timestamp.timestamp())
    bucket = epoch - (epoch % max(interval_seconds, 1))
    return datetime.fromtimestamp(bucket, tz=timezone.utc)


def _heartbeat_due(now: datetime, last_heartbeat_at: datetime | None, interval_seconds: int) -> bool:
    if last_heartbeat_at is None:
        return True
    heartbeat_seconds = max(60, min(300, interval_seconds * 4))
    return (now - last_heartbeat_at).total_seconds() >= heartbeat_seconds


def _db_snapshot_count(storage: Storage) -> int | None:
    try:
        frame = storage.fetch_sql("SELECT COUNT(*) AS count FROM orderbook_snapshots_live")
        return int(frame.iloc[0]["count"]) if not frame.empty else 0
    except Exception:
        return None


def _age_seconds(now: datetime, then: datetime | None) -> float:
    if then is None:
        return -1.0
    return (now - then).total_seconds()
