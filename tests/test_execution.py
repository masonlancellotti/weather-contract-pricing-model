from datetime import datetime, timezone
from decimal import Decimal

from models import Fill, Order, OrderStatus, Side, Venue
from storage import DB
from strategy.execution import sync_orders_from_venue


class DummyKalshiClient:
    def get_open_orders(self):
        return []


def _make_order() -> Order:
    now = datetime.now(timezone.utc)
    return Order(
        venue=Venue.KALSHI,
        venue_order_id="venue-order-1",
        client_order_id="client-order-1",
        market_id="KXHIGHNY-24APR01-T70",
        city="NYC",
        bucket_label="68-72",
        side=Side.YES,
        price=Decimal("0.42"),
        contracts=3,
        status=OrderStatus.OPEN,
        created_at=now,
        updated_at=now,
    )


def test_sync_orders_marks_filled_when_fills_cover_order(tmp_path):
    db = DB(str(tmp_path / "sync.db"))
    db.connect()
    order = _make_order()
    db.upsert_order(order)
    db.insert_fill(
        Fill(
            venue=Venue.KALSHI,
            fill_id="fill-1",
            order_id=order.venue_order_id,
            market_id=order.market_id,
            side=order.side,
            contracts=3,
            price=Decimal("0.44"),
            fee=Decimal("0"),
            ts=datetime.now(timezone.utc),
        )
    )
    sync_orders_from_venue(
        db,
        kalshi_client=DummyKalshiClient(),
        fill_sync_ok={Venue.KALSHI: True, Venue.POLYMARKET: False},
    )
    row = db.get_order_by_client_id(order.client_order_id)
    assert row["status"] == "filled"
    assert row["filled_contracts"] == 3
    db.close()


def test_sync_orders_marks_partial_when_some_fills_exist(tmp_path):
    db = DB(str(tmp_path / "sync_partial.db"))
    db.connect()
    order = _make_order()
    db.upsert_order(order)
    db.insert_fill(
        Fill(
            venue=Venue.KALSHI,
            fill_id="fill-2",
            order_id=order.venue_order_id,
            market_id=order.market_id,
            side=order.side,
            contracts=1,
            price=Decimal("0.44"),
            fee=Decimal("0"),
            ts=datetime.now(timezone.utc),
        )
    )
    sync_orders_from_venue(
        db,
        kalshi_client=DummyKalshiClient(),
        fill_sync_ok={Venue.KALSHI: True, Venue.POLYMARKET: False},
    )
    row = db.get_order_by_client_id(order.client_order_id)
    assert row["status"] == "partially_filled"
    assert row["filled_contracts"] == 1
    db.close()
