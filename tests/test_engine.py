from datetime import datetime, timedelta, timezone
from decimal import Decimal

from models import MarketSnapshot, Venue
from storage import DB
from strategy.engine import StrategyEngine, _row_to_snapshot


def test_row_to_snapshot_preserves_db_timestamp():
    row = {
        "venue": "kalshi",
        "market_id": "KXHIGHNY-24APR01-T70",
        "city": "NYC",
        "bucket_label": "68-72",
        "bucket_min": 68.0,
        "bucket_max": 72.0,
        "settlement_date": "2024-04-01",
        "best_bid": "0.40",
        "best_ask": "0.44",
        "last_price": "0.42",
        "is_open": 1,
        "ts": "2024-04-01T12:00:00+00:00",
    }
    snap = _row_to_snapshot(row)
    assert snap.ts == datetime(2024, 4, 1, 12, 0, tzinfo=timezone.utc)


def test_market_data_freshness_gate_blocks_stale_snapshots(tmp_path):
    db = DB(str(tmp_path / "engine.db"))
    db.connect()
    engine = StrategyEngine(db)
    stale_ts = datetime.now(timezone.utc) - timedelta(minutes=20)
    snap = MarketSnapshot(
        venue=Venue.KALSHI,
        market_id="KXHIGHNY-24APR01-T70",
        city="NYC",
        bucket_label="68-72",
        bucket_min=68.0,
        bucket_max=72.0,
        settlement_date="2024-04-01",
        best_bid=Decimal("0.40"),
        best_ask=Decimal("0.44"),
        last_price=Decimal("0.42"),
        yes_bid=None,
        yes_ask=None,
        ts=stale_ts,
        is_open=True,
    )
    assert not engine._market_data_is_fresh("NYC", [snap])
    db.close()
