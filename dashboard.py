"""
dashboard.py — Streamlit local dashboard.
Reads entirely from SQLite — no direct venue API calls.
Run: streamlit run dashboard.py

Auto-refreshes every 15 seconds.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "weather_trader.db"

st.set_page_config(
    page_title="Weather Trader",
    page_icon="🌡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Auto-refresh
from streamlit_autorefresh import st_autorefresh  # noqa: E402
try:
    st_autorefresh(interval=15_000, key="refresh")
except ImportError:
    st.caption("Install streamlit-autorefresh for live refresh")


# ── DB helpers ────────────────────────────────────────────────────────────────

@st.cache_resource
def get_conn():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql: str, params=()) -> list[dict]:
    try:
        conn = get_conn()
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        st.error(f"DB error: {e}")
        return []


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🌡️ Weather Trader")
    st.markdown("---")

    # Live / Paper status
    try:
        from config import cfg
        live_status = "🔴 LIVE" if cfg.live_enabled else "🟡 PAPER"
        kill_status = "🛑 KILL SWITCH ON" if cfg.kill_switch else "✅ Kill switch OFF"
    except Exception:
        live_status = "⚠️ Config unavailable"
        kill_status = ""

    st.metric("Mode", live_status)
    st.caption(kill_status)

    from strategy.risk import is_safe_mode, safe_mode_reason
    if is_safe_mode():
        st.error(f"⚠️ SAFE MODE: {safe_mode_reason()}")

    st.markdown("---")
    st.caption(f"DB: {DB_PATH.name}")
    st.caption(f"Refreshed: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")


# ── Balances ─────────────────────────────────────────────────────────────────

st.header("Balances")
col1, col2 = st.columns(2)

with col1:
    st.subheader("Kalshi")
    try:
        from config import cfg
        from venues.kalshi import KalshiClient
        if cfg.has_kalshi_creds():
            @st.cache_data(ttl=30)
            def get_kalshi_balance():
                c = KalshiClient()
                return c.get_balance()
            b = get_kalshi_balance()
            st.metric("Available", f"${b.available:.2f}")
            st.metric("Total", f"${b.total:.2f}")
    except Exception as e:
        st.caption(f"Unavailable: {e}")

with col2:
    st.subheader("Polymarket")
    try:
        from config import cfg
        from venues.polymarket import PolymarketClient
        if cfg.has_poly_creds():
            @st.cache_data(ttl=30)
            def get_poly_balance():
                c = PolymarketClient()
                return c.get_balance()
            b = get_poly_balance()
            st.metric("Available (USDC)", f"${b.available:.2f}")
    except Exception as e:
        st.caption(f"Unavailable: {e}")


# ── Open Orders ───────────────────────────────────────────────────────────────

st.header("Open Orders")
orders = q("""
    SELECT venue, market_id, city, bucket_label, side, price, contracts,
           status, created_at
    FROM orders
    WHERE status IN ('open', 'partially_filled')
    ORDER BY created_at DESC
""")
if orders:
    df = pd.DataFrame(orders)
    df["price"] = df["price"].apply(lambda x: f"{float(x):.3f}")
    st.dataframe(df, use_container_width=True)
else:
    st.caption("No open orders")


# ── Positions ─────────────────────────────────────────────────────────────────

st.header("Positions")
positions = q("""
    SELECT venue, market_id, city, bucket_label, side, contracts, avg_price, snapped_at
    FROM positions_snapshot
    WHERE contracts > 0
    ORDER BY snapped_at DESC
""")
if positions:
    df = pd.DataFrame(positions)
    df["avg_price"] = df["avg_price"].apply(lambda x: f"{float(x):.3f}")
    st.dataframe(df, use_container_width=True)
else:
    st.caption("No open positions")


# ── PnL Summary ──────────────────────────────────────────────────────────────

st.header("PnL")
col1, col2 = st.columns(2)

with col1:
    fills = q("""
        SELECT venue,
               SUM(CASE WHEN side='yes' THEN contracts * price ELSE 0 END) as yes_cost,
               SUM(fee) as total_fees,
               COUNT(*) as num_fills
        FROM fills
        GROUP BY venue
    """)
    if fills:
        st.dataframe(pd.DataFrame(fills), use_container_width=True)
    else:
        st.caption("No fills yet")

with col2:
    # Realized PnL = (sell proceeds - buy cost) from closed positions
    # Simplified: show total fees paid
    fee_total = q("SELECT COALESCE(SUM(fee), 0) as total_fees FROM fills")
    if fee_total:
        st.metric("Total Fees Paid", f"${float(fee_total[0]['total_fees']):.4f}")


# ── Market Monitor ────────────────────────────────────────────────────────────

st.header("Market Monitor")

cities = q("SELECT DISTINCT city FROM market_snapshots WHERE is_open=1 ORDER BY city")
tabs = st.tabs([c["city"] for c in cities] or ["No cities"])

for i, city_row in enumerate(cities):
    city = city_row["city"]
    with tabs[i]:
        # Latest snapshots per market
        snaps = q("""
            SELECT ms.venue, ms.market_id, ms.bucket_label,
                   ms.best_bid, ms.best_ask, ms.last_price,
                   ms.settlement_date, ms.ts
            FROM market_snapshots ms
            INNER JOIN (
                SELECT market_id, venue, MAX(ts) as max_ts
                FROM market_snapshots
                WHERE city=? AND is_open=1
                GROUP BY market_id, venue
            ) latest ON ms.market_id=latest.market_id
                    AND ms.venue=latest.venue
                    AND ms.ts=latest.max_ts
            ORDER BY ms.venue, ms.bucket_min
        """, (city,))

        if snaps:
            df = pd.DataFrame(snaps)

            # Get model output from weather cache
            model_row = q(
                "SELECT data_json FROM weather_cache WHERE city=? AND data_type='model'",
                (city,)
            )
            if model_row:
                model = json.loads(model_row[0]["data_json"])
                bucket_probs = model.get("bucket_probs", {})

                st.markdown(
                    f"**Model**: expected high = **{model['expected_high']:.1f}°F** "
                    f"± {model['std_dev']:.1f}°F | "
                    f"confidence = {model['confidence']:.0%}"
                )

                # Add model probability column
                df["model_prob"] = df["bucket_label"].apply(
                    lambda lbl: f"{bucket_probs.get(lbl, 0):.3f}"
                )

            # Format prices
            for col in ["best_bid", "best_ask", "last_price"]:
                df[col] = df[col].apply(lambda x: f"{float(x):.3f}" if x else "-")

            st.dataframe(df, use_container_width=True)

            # Weather inputs
            obs_row = q(
                "SELECT data_json FROM weather_cache WHERE city=? AND data_type='obs'",
                (city,)
            )
            fc_row = q(
                "SELECT data_json FROM weather_cache WHERE city=? AND data_type='forecast'",
                (city,)
            )
            wc1, wc2 = st.columns(2)
            if obs_row:
                obs = json.loads(obs_row[0]["data_json"])
                with wc1:
                    st.metric(
                        f"Current Obs ({obs.get('source', 'nws')})",
                        f"{obs.get('temp_f', '?'):.1f}°F"
                    )
            if fc_row:
                fc = json.loads(fc_row[0]["data_json"])
                with wc2:
                    st.metric(
                        f"Forecast High ({fc.get('source', 'nws')})",
                        f"{fc.get('high_f', '?'):.1f}°F"
                    )
        else:
            st.caption(f"No active markets for {city}")


# ── Recent Fills ─────────────────────────────────────────────────────────────

st.header("Recent Fills")
recent_fills = q("""
    SELECT venue, fill_id, market_id, side, contracts, price, fee, ts
    FROM fills
    ORDER BY ts DESC
    LIMIT 50
""")
if recent_fills:
    st.dataframe(pd.DataFrame(recent_fills), use_container_width=True)
else:
    st.caption("No fills")


# ── Recent Logs ──────────────────────────────────────────────────────────────

st.header("Logs (WARN+)")
logs = q("""
    SELECT ts, level, msg
    FROM logs
    ORDER BY ts DESC
    LIMIT 100
""")
if logs:
    for row in logs:
        level = row["level"]
        color = {"ERROR": "🔴", "WARNING": "🟡", "CRITICAL": "🚨"}.get(level, "⚪")
        st.markdown(f"`{row['ts'][:19]}` {color} **{level}** — {row['msg'][:200]}")
else:
    st.caption("No warnings/errors logged")


# ── Heartbeat ────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("---")
    st.subheader("Health")
    # Show timestamp of most recent market snapshot
    latest_snap = q(
        "SELECT MAX(ts) as last_snap FROM market_snapshots"
    )
    if latest_snap and latest_snap[0]["last_snap"]:
        last_ts = latest_snap[0]["last_snap"]
        dt = datetime.fromisoformat(last_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
        color = "🟢" if age < 60 else ("🟡" if age < 300 else "🔴")
        st.caption(f"{color} Last market data: {int(age)}s ago")
    else:
        st.caption("⚫ No market data yet")

    latest_log = q("SELECT MAX(ts) as last_log FROM logs WHERE level='ERROR'")
    if latest_log and latest_log[0]["last_log"]:
        st.caption(f"🔴 Last error: {latest_log[0]['last_log'][:19]}")
