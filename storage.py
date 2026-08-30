"""
UTC = timezone.utc

storage.py — SQLite persistence layer. Enough to recover state after restart.
One connection, WAL mode, no ORM.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from models import Balance, Fill, MarketSnapshot, Order, OrderStatus, Position, Side, Venue


def _adapt_decimal(d: Decimal) -> str:
    return str(d)


def _convert_decimal(s: bytes) -> Decimal:
    return Decimal(s.decode())


sqlite3.register_adapter(Decimal, _adapt_decimal)
sqlite3.register_converter("DECIMAL", _convert_decimal)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    venue           TEXT NOT NULL,
    venue_order_id  TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    market_id       TEXT NOT NULL,
    city            TEXT NOT NULL,
    bucket_label    TEXT NOT NULL,
    side            TEXT NOT NULL,
    price           DECIMAL NOT NULL,
    contracts       INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    filled_contracts INTEGER NOT NULL DEFAULT 0,
    fill_price      DECIMAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    venue       TEXT NOT NULL,
    fill_id     TEXT NOT NULL UNIQUE,
    order_id    TEXT NOT NULL,
    market_id   TEXT NOT NULL,
    side        TEXT NOT NULL,
    contracts   INTEGER NOT NULL,
    price       DECIMAL NOT NULL,
    fee         DECIMAL NOT NULL,
    ts          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions_snapshot (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    venue        TEXT NOT NULL,
    market_id    TEXT NOT NULL,
    city         TEXT NOT NULL,
    bucket_label TEXT NOT NULL,
    side         TEXT NOT NULL,
    contracts    INTEGER NOT NULL,
    avg_price    DECIMAL NOT NULL,
    snapped_at   TEXT NOT NULL,
    UNIQUE(venue, market_id)
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    venue        TEXT NOT NULL,
    market_id    TEXT NOT NULL,
    city         TEXT NOT NULL,
    bucket_label TEXT NOT NULL,
    bucket_min   REAL,
    bucket_max   REAL,
    settlement_date TEXT NOT NULL,
    best_bid     DECIMAL,
    best_ask     DECIMAL,
    last_price   DECIMAL,
    is_open      INTEGER NOT NULL DEFAULT 1,
    ts           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weather_cache (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    city       TEXT NOT NULL,
    data_type  TEXT NOT NULL,   -- 'obs', 'forecast', 'history'
    data_json  TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE(city, data_type)
);

CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    level   TEXT NOT NULL,
    msg     TEXT NOT NULL,
    ts      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_venue_status ON orders(venue, status);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_market_snaps_city ON market_snapshots(city, settlement_date);
"""


class DB:
    def __init__(self, path: str = "./weather_trader.db"):
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self):
        self._conn = sqlite3.connect(
            self.path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("DB not connected — call db.connect() first")
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Orders ───────────────────────────────────────────────────────────────────

    def upsert_order(self, o: Order):
        self.conn.execute(
            """
            INSERT INTO orders
              (venue, venue_order_id, client_order_id, market_id, city, bucket_label,
               side, price, contracts, status, filled_contracts, fill_price, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(client_order_id) DO UPDATE SET
              status=excluded.status,
              filled_contracts=excluded.filled_contracts,
              fill_price=excluded.fill_price,
              updated_at=excluded.updated_at,
              venue_order_id=excluded.venue_order_id
            """,
            (
                o.venue.value, o.venue_order_id, o.client_order_id, o.market_id,
                o.city, o.bucket_label, o.side.value, o.price, o.contracts,
                o.status.value, o.filled_contracts, o.fill_price,
                o.created_at.isoformat(), o.updated_at.isoformat(),
            ),
        )
        self.conn.commit()

    def get_open_orders(self, venue: Optional[Venue] = None) -> list[sqlite3.Row]:
        if venue:
            return self.conn.execute(
                "SELECT * FROM orders WHERE status IN ('open','partially_filled') AND venue=?",
                (venue.value,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM orders WHERE status IN ('open','partially_filled')"
        ).fetchall()

    def get_order_by_client_id(self, client_order_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)
        ).fetchone()

    def mark_order_cancelled(self, client_order_id: str):
        self.conn.execute(
            "UPDATE orders SET status='cancelled', updated_at=? WHERE client_order_id=?",
            (datetime.now(timezone.utc).isoformat(), client_order_id),
        )
        self.conn.commit()

    def mark_order_status(
        self,
        client_order_id: str,
        status: OrderStatus,
        filled_contracts: Optional[int] = None,
        fill_price: Optional[Decimal] = None,
    ):
        self.conn.execute(
            """
            UPDATE orders
            SET status=?,
                filled_contracts=COALESCE(?, filled_contracts),
                fill_price=COALESCE(?, fill_price),
                updated_at=?
            WHERE client_order_id=?
            """,
            (
                status.value,
                filled_contracts,
                fill_price,
                datetime.now(timezone.utc).isoformat(),
                client_order_id,
            ),
        )
        self.conn.commit()

    def get_recent_fills(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    def get_order_fill_stats(self, venue_order_id: str) -> tuple[int, Optional[Decimal]]:
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(contracts), 0) AS filled_contracts,
                   CASE
                       WHEN COALESCE(SUM(contracts), 0) = 0 THEN NULL
                       ELSE SUM(price * contracts) / SUM(contracts)
                   END AS avg_fill_price
            FROM fills
            WHERE order_id=?
            """,
            (venue_order_id,),
        ).fetchone()
        if not row:
            return 0, None
        avg_fill_price = (
            Decimal(str(row["avg_fill_price"])) if row["avg_fill_price"] is not None else None
        )
        return int(row["filled_contracts"]), avg_fill_price

    # ── Fills ────────────────────────────────────────────────────────────────────

    def insert_fill(self, f: Fill):
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO fills
                   (venue, fill_id, order_id, market_id, side, contracts, price, fee, ts)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (f.venue.value, f.fill_id, f.order_id, f.market_id,
                 f.side.value, f.contracts, f.price, f.fee, f.ts.isoformat()),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass  # already seen this fill

    # ── Positions ────────────────────────────────────────────────────────────────

    def upsert_position(self, p: Position):
        self.conn.execute(
            """INSERT INTO positions_snapshot
               (venue, market_id, city, bucket_label, side, contracts, avg_price, snapped_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(venue, market_id) DO UPDATE SET
                 contracts=excluded.contracts,
                 avg_price=excluded.avg_price,
                 side=excluded.side,
                 snapped_at=excluded.snapped_at""",
            (p.venue.value, p.market_id, p.city, p.bucket_label,
             p.side.value, p.contracts, p.avg_price, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def get_positions(self, venue: Optional[Venue] = None) -> list[sqlite3.Row]:
        if venue:
            return self.conn.execute(
                "SELECT * FROM positions_snapshot WHERE venue=? AND contracts > 0",
                (venue.value,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM positions_snapshot WHERE contracts > 0"
        ).fetchall()

    # ── Market snapshots ─────────────────────────────────────────────────────────

    def upsert_market_snapshot(self, s: MarketSnapshot):
        self.conn.execute(
            """INSERT INTO market_snapshots
               (venue, market_id, city, bucket_label, bucket_min, bucket_max,
                settlement_date, best_bid, best_ask, last_price, is_open, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s.venue.value, s.market_id, s.city, s.bucket_label,
             s.bucket_min, s.bucket_max, s.settlement_date,
             s.best_bid, s.best_ask, s.last_price,
             int(s.is_open), s.ts.isoformat()),
        )
        self.conn.commit()

    def get_latest_snapshots(self, city: str, settlement_date: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT ms.* FROM market_snapshots ms
               INNER JOIN (
                 SELECT market_id, venue, MAX(ts) as max_ts
                 FROM market_snapshots
                 WHERE city=? AND settlement_date=?
                 GROUP BY market_id, venue
               ) latest ON ms.market_id=latest.market_id
                       AND ms.venue=latest.venue
                       AND ms.ts=latest.max_ts""",
            (city, settlement_date),
        ).fetchall()

    # ── Weather cache ────────────────────────────────────────────────────────────

    def set_weather(self, city: str, data_type: str, data: dict):
        self.conn.execute(
            """INSERT INTO weather_cache (city, data_type, data_json, fetched_at)
               VALUES (?,?,?,?)
               ON CONFLICT(city, data_type) DO UPDATE SET
                 data_json=excluded.data_json,
                 fetched_at=excluded.fetched_at""",
            (city, data_type, json.dumps(data), datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def get_weather(self, city: str, data_type: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT data_json FROM weather_cache WHERE city=? AND data_type=?",
            (city, data_type),
        ).fetchone()
        if row:
            return json.loads(row["data_json"])
        return None

    # ── Logs ─────────────────────────────────────────────────────────────────────

    def insert_log(self, level: str, msg: str):
        self.conn.execute(
            "INSERT INTO logs (level, msg, ts) VALUES (?,?,?)",
            (level, msg, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def get_recent_logs(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM logs ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    # ── Computed helpers ─────────────────────────────────────────────────────────

    def get_venue_exposure(self, venue: Venue) -> Decimal:
        """Backward-compatible alias for total venue exposure."""
        return self.get_total_exposure(venue)

    def get_total_exposure(self, venue: Venue) -> Decimal:
        return self._get_order_exposure(venue=venue) + self._get_position_exposure(venue=venue)

    def get_city_exposure(self, venue: Venue, city: str) -> Decimal:
        return self._get_order_exposure(venue=venue, city=city) + self._get_position_exposure(
            venue=venue, city=city
        )

    def get_market_side_exposure(self, venue: Venue, market_id: str, side: Side) -> Decimal:
        return self._get_order_exposure(
            venue=venue, market_id=market_id, side=side
        ) + self._get_position_exposure(venue=venue, market_id=market_id, side=side)

    def _get_order_exposure(
        self,
        venue: Venue,
        city: Optional[str] = None,
        market_id: Optional[str] = None,
        side: Optional[Side] = None,
    ) -> Decimal:
        sql = (
            "SELECT price, contracts FROM orders "
            "WHERE venue=? AND status IN ('open','partially_filled')"
        )
        params: list[object] = [venue.value]
        if city:
            sql += " AND city=?"
            params.append(city)
        if market_id:
            sql += " AND market_id=?"
            params.append(market_id)
        if side:
            sql += " AND side=?"
            params.append(side.value)
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        total = Decimal("0")
        for r in rows:
            total += Decimal(str(r["price"])) * r["contracts"]
        return total

    def _get_position_exposure(
        self,
        venue: Venue,
        city: Optional[str] = None,
        market_id: Optional[str] = None,
        side: Optional[Side] = None,
    ) -> Decimal:
        sql = (
            "SELECT avg_price, contracts FROM positions_snapshot "
            "WHERE venue=? AND contracts > 0"
        )
        params: list[object] = [venue.value]
        if city:
            sql += " AND city=?"
            params.append(city)
        if market_id:
            sql += " AND market_id=?"
            params.append(market_id)
        if side:
            sql += " AND side=?"
            params.append(side.value)
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        total = Decimal("0")
        for r in rows:
            total += Decimal(str(r["avg_price"])) * r["contracts"]
        return total


# Module-level singleton
db = DB()
