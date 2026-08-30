"""
terminal_dashboard.py - Rich terminal dashboard for Weather Trader.

Run:
    python terminal_dashboard.py
    python terminal_dashboard.py --refresh 2
    python terminal_dashboard.py --once
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import cfg
from models import MarketSnapshot, ModelOutput, Venue
from storage import DB
from strategy.pricing import analyze_market, compute_edge
from strategy.risk import is_safe_mode, safe_mode_reason
from strategy.sizing import size_signal
from utils.time import (
    can_open_new_same_day_positions,
    city_local_now,
    hours_until_entry_cutoff,
    hours_until_entry_start,
)
from weather.mapping import active_cities

DB_PATH = Path(cfg.db_path).resolve()
UTC = timezone.utc
SPARK_CHARS = "._-~=*#@"


@dataclass
class BalanceSnapshot:
    kalshi_available: Optional[float] = None
    kalshi_total: Optional[float] = None
    poly_available: Optional[float] = None
    poly_total: Optional[float] = None

    @property
    def cash_available(self) -> Optional[float]:
        values = [value for value in (self.kalshi_available, self.poly_available) if value is not None]
        return sum(values) if values else None

    @property
    def account_value(self) -> Optional[float]:
        values = []
        for total, available in (
            (self.kalshi_total, self.kalshi_available),
            (self.poly_total, self.poly_available),
        ):
            if total is not None and total > 0:
                values.append(total)
            elif available is not None:
                values.append(available)
        return sum(values) if values else None


@dataclass
class RadarRow:
    city: str
    bucket: str
    side: str
    market_prob: float
    model_prob: float
    edge: float
    threshold: float
    size: int
    local_time: str
    reason: str
    kind: str

    @property
    def gap(self) -> float:
        return self.threshold - self.edge


@dataclass
class CityView:
    city: str
    display_name: str
    local_time: str
    entry_open: bool
    expected_high: Optional[float]
    confidence: Optional[float]
    obs_temp: Optional[float]
    observed_high: Optional[float]
    forecast_high: Optional[float]
    remaining_forecast_high: Optional[float]
    focus_bucket: str
    focus_side: str
    focus_edge: Optional[float]
    focus_market: Optional[float]
    focus_model: Optional[float]
    spark: str
    signal_count: int
    watch_count: int
    monitored_markets: int
    border_style: str
    subtitle: str


@dataclass
class MacroTicker:
    symbol: str
    name: str
    value: float
    change_pct: float
    theme: str


@dataclass
class MacroBoard:
    tickers: list[MacroTicker]
    note: str


@dataclass
class SessionPulse:
    open_cities: int
    closed_cities: int
    avg_confidence: Optional[float]
    best_edge: Optional[float]
    watch_gap: Optional[float]
    hottest_city: Optional[str]
    hottest_temp: Optional[float]
    observed_coverage: int
    expected_coverage: int
    guardrail_alerts: int
    signals_live: int
    watch_live: int


class TerminalDashboard:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.console = Console()
        self.balance_cache: tuple[float, BalanceSnapshot] = (0.0, BalanceSnapshot())
        self.context_cache: tuple[float, MacroBoard] = (0.0, self._fallback_macro_board("boot cache"))
        self.sizing_db = DB(str(db_path))
        self.sizing_db.connect()

    def close(self):
        self.conn.close()
        self.sizing_db.close()

    def q(self, sql: str, params=()) -> list[dict]:
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def render(self):
        state = self._build_state()
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=4),
            Layout(name="summary", size=10),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=12),
        )
        layout["main"].split_row(
            Layout(name="left", ratio=7),
            Layout(name="center", ratio=4),
            Layout(name="right", ratio=5),
        )
        layout["left"].split_column(
            Layout(name="radar", size=18),
            Layout(name="cities", ratio=1),
        )
        layout["center"].split_column(
            Layout(name="pulse", size=11),
            Layout(name="watchtower", size=15),
            Layout(name="matrix", ratio=1),
        )
        layout["right"].split_column(
            Layout(name="portfolio", size=12),
            Layout(name="context", size=10),
            Layout(name="lens", ratio=1),
        )
        layout["footer"].split_row(
            Layout(name="orders", ratio=2),
            Layout(name="fills", ratio=1),
            Layout(name="alerts", ratio=2),
        )

        layout["header"].update(self._render_header(state))
        layout["summary"].update(self._render_summary(state))
        layout["radar"].update(self._render_radar(state))
        layout["cities"].update(self._render_cities(state))
        layout["pulse"].update(self._render_pulse(state))
        layout["watchtower"].update(self._render_watchtower(state))
        layout["matrix"].update(self._render_city_matrix(state))
        layout["portfolio"].update(self._render_portfolio(state))
        layout["context"].update(self._render_context(state))
        layout["lens"].update(self._render_lens(state))
        layout["orders"].update(self._render_orders(state))
        layout["fills"].update(self._render_fills(state))
        layout["alerts"].update(self._render_alerts(state))
        return layout

    def _build_state(self) -> dict:
        balances = self._get_balances()
        positions = self.q(
            """
            SELECT venue, market_id, city, bucket_label, side, contracts, avg_price, snapped_at
            FROM positions_snapshot
            WHERE contracts > 0
            ORDER BY snapped_at DESC
            """
        )
        recent_logs = self.q(
            """
            SELECT level, msg, ts
            FROM logs
            ORDER BY ts DESC
            LIMIT 18
            """
        )
        open_orders = self.q(
            """
            SELECT venue, city, bucket_label, side, price, contracts, status, created_at
            FROM orders
            WHERE status IN ('open', 'partially_filled')
            ORDER BY created_at DESC
            LIMIT 12
            """
        )
        recent_fills = self.q(
            """
            SELECT venue, market_id, side, contracts, price, fee, ts
            FROM fills
            ORDER BY ts DESC
            LIMIT 12
            """
        )

        signals: list[RadarRow] = []
        watchlist: list[RadarRow] = []
        city_views: list[CityView] = []
        market_tape: list[dict] = []
        monitored_markets = 0

        for meta in active_cities():
            model = self._load_model(meta.city_code)
            snapshots = self._load_latest_open_snapshots(meta.city_code)
            monitored_markets += len(snapshots)
            city_rows = self._analyze_city(meta.city_code, model, snapshots)
            signals.extend(city_rows["signals"])
            watchlist.extend(city_rows["watchlist"])
            city_views.append(self._build_city_view(meta.city_code, meta.display_name, model, snapshots, city_rows))
            tape_row = self._market_tape_row(meta.city_code, snapshots)
            if tape_row:
                market_tape.append(tape_row)

        signals.sort(key=lambda row: row.edge, reverse=True)
        watchlist = [row for row in watchlist if row.edge > 0]
        watchlist.sort(key=lambda row: (row.gap, -row.edge, row.city))

        portfolio = self._portfolio_snapshot(balances, positions)
        portfolio["fills_today"] = self._fills_today_count()
        portfolio["open_orders"] = len(open_orders)
        portfolio["positions_count"] = len(positions)
        portfolio["markets_monitored"] = monitored_markets
        portfolio["active_cities"] = len(active_cities())
        portfolio["errors"] = sum(1 for row in recent_logs if row["level"] in {"ERROR", "CRITICAL"})
        portfolio["fees_today"] = sum(float(row["fee"]) for row in recent_fills if _same_day_utc(row["ts"]))
        portfolio["exposure"] = self._total_open_exposure()

        return {
            "status": self._system_status(),
            "balances": balances,
            "portfolio": portfolio,
            "pulse": self._session_pulse(city_views, signals, watchlist, recent_logs),
            "signals": signals[:10],
            "watchlist": watchlist[:10],
            "city_views": city_views,
            "market_tape": market_tape[:10],
            "macro_board": self._context_board(),
            "open_orders": open_orders,
            "recent_fills": recent_fills,
            "recent_logs": recent_logs,
            "top_focus": signals[0] if signals else (watchlist[0] if watchlist else None),
            "windows": self._upcoming_windows(),
        }

    def _render_header(self, state: dict):
        status = state["status"]
        title = Text()
        title.append(" WEATHER TRADER ", style="bold black on bright_cyan")
        title.append(" CONTROL CENTER ", style="bold bright_white on blue")
        title.append(f"  {status['live_mode']}  ", style=_mode_style(status["live_mode"]))
        title.append(f"safe={status['safe']}  ", style="bold bright_white")
        title.append(f"kill={status['kill']}  ", style="bold bright_white")
        title.append(f"market_age={status['market_age']}  ", style="cyan")
        title.append(
            f"entry={cfg.entry_start_local_hour:02d}:00-{cfg.entry_cutoff_local_hour:02d}:00 local",
            style="bright_green",
        )

        tape = Text()
        for city in state["city_views"]:
            tape.append(
                f" {city.city} {city.local_time} {'OPEN' if city.entry_open else 'CLOSED'} ",
                style="bold black on green" if city.entry_open else "bold white on red",
            )
        return Panel(Group(title, tape), box=box.HEAVY, border_style="bright_cyan")

    def _render_summary(self, state: dict):
        portfolio = state["portfolio"]
        balances = state["balances"]
        cards_row_one = Columns(
            [
                _metric_card("Account Value", _fmt_money(portfolio["account_value"]), "live balance", "bright_cyan"),
                _metric_card("Cash Available", _fmt_money(portfolio["cash_available"]), "deployable", "bright_blue"),
                _metric_card("Daily P&L*", _fmt_signed_money(portfolio["daily_pnl"]), "marked today", _pnl_color(portfolio["daily_pnl"])),
                _metric_card("Unrealized", _fmt_signed_money(portfolio["unrealized_pnl"]), "open positions", _pnl_color(portfolio["unrealized_pnl"])),
                _metric_card("Open Exposure", _fmt_money(portfolio["exposure"]), "working capital", "yellow"),
            ],
            expand=True,
            equal=True,
        )
        cards_row_two = Columns(
            [
                _metric_card("Open Orders", str(portfolio["open_orders"]), "blotter", "bright_cyan"),
                _metric_card("Positions", str(portfolio["positions_count"]), "live holdings", "magenta"),
                _metric_card("Fills Today", str(portfolio["fills_today"]), f"fees {_fmt_money(portfolio['fees_today'])}", "green"),
                _metric_card("Coverage", f"{portfolio['active_cities']}c / {portfolio['markets_monitored']}m", "cities / markets", "bright_white"),
                _metric_card("Bot State", f"{state['status']['live_mode']} | {state['status']['safe']}", f"kill {state['status']['kill']}", "red" if state["status"]["kill"] == "ON" else "bright_green"),
            ],
            expand=True,
            equal=True,
        )
        return Group(
            Panel(cards_row_one, title="[bold bright_white]Account Snapshot[/bold bright_white]", border_style="bright_blue", box=box.SQUARE),
            Panel(cards_row_two, title="[bold bright_white]Execution Snapshot[/bold bright_white]", border_style="bright_magenta", box=box.SQUARE),
        )

    def _render_radar(self, state: dict):
        signal_table = Table(box=box.SIMPLE_HEAVY, expand=True, title="Trade Radar")
        signal_table.add_column("City", style="bright_cyan", width=6)
        signal_table.add_column("Bucket", style="bright_white", min_width=10)
        signal_table.add_column("Signal", width=8)
        signal_table.add_column("Mkt", justify="right")
        signal_table.add_column("Model", justify="right")
        signal_table.add_column("Edge", justify="right")
        signal_table.add_column("Qty", justify="right")
        signal_table.add_column("Local", justify="right")
        signal_table.add_column("State", width=10)

        if state["signals"]:
            for row in state["signals"][:8]:
                signal_table.add_row(
                    row.city,
                    row.bucket[:18],
                    Text(f"BUY {row.side}", style=_side_style(row.side)),
                    _fmt_prob(row.market_prob),
                    _fmt_prob(row.model_prob),
                    f"{row.edge:.3f}",
                    str(row.size),
                    row.local_time,
                    Text("ACTIVE", style="bold green"),
                )
        else:
            signal_table.add_row(
                "-",
                "No active entries",
                Text("STANDBY", style="bold yellow"),
                "-",
                "-",
                "-",
                "-",
                "-",
                Text("watch queue below", style="yellow"),
            )
        subtitle = "active entries prioritized by edge and executable size"
        if not state["signals"]:
            subtitle = "no live entries - watchtower is tracking near-threshold setups"
        return Panel(signal_table, title="[bold bright_white]Trade Radar[/bold bright_white]", subtitle=subtitle, border_style="green", box=box.SQUARE)

    def _render_cities(self, state: dict):
        panels = [self._city_panel(city) for city in state["city_views"]]
        return Columns(panels, expand=True, equal=True, padding=(0, 1))

    def _render_portfolio(self, state: dict):
        portfolio = state["portfolio"]
        curve = portfolio["curve"]
        curve_text = Text(_sparkline(curve), style="bright_cyan")
        stats = Table.grid(expand=True)
        stats.add_column(style="bright_white")
        stats.add_column(justify="right", style="bold bright_white")
        stats.add_row("Account", _fmt_money(portfolio["account_value"]))
        stats.add_row("Cash", _fmt_money(portfolio["cash_available"]))
        stats.add_row("Marked P&L", Text(_fmt_signed_money(portfolio["daily_pnl"]), style=_pnl_color(portfolio["daily_pnl"])))
        stats.add_row("Unrealized", Text(_fmt_signed_money(portfolio["unrealized_pnl"]), style=_pnl_color(portfolio["unrealized_pnl"])))

        positions_table = Table(box=box.MINIMAL, expand=True, title="Live Marks")
        positions_table.add_column("City", width=6, style="bright_cyan")
        positions_table.add_column("Bucket", min_width=9)
        positions_table.add_column("Side", width=5)
        positions_table.add_column("Mark", justify="right")
        positions_table.add_column("PnL", justify="right")
        if portfolio["position_rows"]:
            for row in portfolio["position_rows"][:5]:
                positions_table.add_row(
                    row["city"],
                    row["bucket"][:16],
                    row["side"],
                    _fmt_prob(row["mark_prob"]),
                    Text(_fmt_signed_money(row["pnl"]), style=_pnl_color(row["pnl"])),
                )
        else:
            positions_table.add_row("-", "No open positions", "-", "-", "-")

        subtitle = f"curve {len(curve)} pts | * marked estimate"
        return Panel(Group(stats, curve_text, positions_table), title="[bold bright_white]Portfolio Command[/bold bright_white]", subtitle=subtitle, border_style="bright_cyan", box=box.SQUARE)

    def _render_pulse(self, state: dict):
        pulse = state["pulse"]
        grid = Table.grid(expand=True)
        grid.add_column(style="bright_white")
        grid.add_column(justify="right", style="bold bright_white")
        grid.add_row("Entry Window", f"{pulse.open_cities} open / {pulse.closed_cities} closed")
        grid.add_row("Avg Confidence", _fmt_prob(pulse.avg_confidence))
        grid.add_row("Best Edge", f"{pulse.best_edge:.3f}" if pulse.best_edge is not None else "-")
        grid.add_row("Nearest Watch", f"{pulse.watch_gap:.3f}" if pulse.watch_gap is not None else "-")
        grid.add_row("Observed Feed", f"{pulse.observed_coverage}/{pulse.expected_coverage}")
        grid.add_row("Guardrails", str(pulse.guardrail_alerts))
        grid.add_row("Signals / Watch", f"{pulse.signals_live} / {pulse.watch_live}")
        hottest = f"{pulse.hottest_city} {_fmt_temp(pulse.hottest_temp)}" if pulse.hottest_city and pulse.hottest_temp is not None else "-"
        grid.add_row("Hottest City", hottest)

        band = Text()
        band.append("CALM ", style="bold black on green")
        band.append("WATCH ", style="bold black on yellow")
        band.append("ARMED ", style="bold white on red" if pulse.signals_live else "white on grey23")
        return Panel(Group(grid, band), title="[bold bright_white]Session Pulse[/bold bright_white]", border_style="bright_magenta", box=box.SQUARE)

    def _render_watchtower(self, state: dict):
        watch_table = Table(box=box.MINIMAL_HEAVY_HEAD, expand=True, title="Watch Queue")
        watch_table.add_column("City", style="bright_cyan", width=6)
        watch_table.add_column("Bucket", min_width=10)
        watch_table.add_column("Bias", width=7)
        watch_table.add_column("Edge", justify="right")
        watch_table.add_column("Gap", justify="right")
        watch_table.add_column("Mkt", justify="right")
        watch_table.add_column("Model", justify="right")
        if state["watchlist"]:
            for row in state["watchlist"][:6]:
                watch_table.add_row(
                    row.city,
                    row.bucket[:18],
                    Text(row.side, style=_side_style(row.side)),
                    f"{row.edge:.3f}",
                    f"{max(row.gap, 0):.3f}",
                    _fmt_prob(row.market_prob),
                    _fmt_prob(row.model_prob),
                )
        else:
            watch_table.add_row("-", "No near-threshold setups", "-", "-", "-", "-", "-")

        windows_table = Table(box=box.MINIMAL_HEAVY_HEAD, expand=True, title="Entry Windows")
        windows_table.add_column("City", style="bright_cyan", width=6)
        windows_table.add_column("Local", justify="right", width=6)
        windows_table.add_column("Window")
        windows_table.add_column("Clock", justify="right", width=12)
        for row in state["windows"][:6]:
            windows_table.add_row(row["city"], row["local"], row["state"], row["clock"])

        return Group(
            Panel(watch_table, border_style="yellow", box=box.SQUARE),
            Panel(windows_table, border_style="bright_blue", box=box.SQUARE),
        )

    def _render_city_matrix(self, state: dict):
        table = Table(box=box.MINIMAL_HEAVY_HEAD, expand=True, title="City Matrix")
        table.add_column("City", style="bright_cyan", width=6)
        table.add_column("Local", justify="right", width=5)
        table.add_column("Gate", width=6)
        table.add_column("Obs", justify="right")
        table.add_column("Hi", justify="right")
        table.add_column("Exp", justify="right")
        table.add_column("Focus", min_width=9)
        table.add_column("Bias", width=5)
        table.add_column("Edge", justify="right")
        table.add_column("Flow", justify="right", width=7)
        for city in state["city_views"][:12]:
            gate = Text("OPEN", style="bold black on green") if city.entry_open else Text("CLOSED", style="bold white on red")
            bias = Text(city.focus_side[:5], style=_side_style(city.focus_side))
            edge = f"{city.focus_edge:.3f}" if city.focus_edge is not None else "-"
            flow = f"{city.signal_count}/{city.watch_count}"
            table.add_row(
                city.city,
                city.local_time,
                gate,
                _fmt_temp(city.obs_temp),
                _fmt_temp(city.observed_high),
                _fmt_temp(city.expected_high),
                city.focus_bucket[:16],
                bias,
                edge,
                flow,
            )
        return Panel(table, border_style="bright_cyan", box=box.SQUARE)

    def _render_context(self, state: dict):
        board = state["macro_board"]
        table = Table(box=box.MINIMAL_HEAVY_HEAD, expand=True, title="Macro Board")
        table.add_column("Ticker", style="bright_cyan", width=6)
        table.add_column("Theme", style="bright_white", width=10)
        table.add_column("Last", justify="right", width=9)
        table.add_column("Chg %", justify="right", width=8)
        for ticker in board.tickers:
            table.add_row(
                ticker.symbol,
                ticker.theme,
                f"{ticker.value:.2f}",
                Text(f"{ticker.change_pct:+.2f}%", style=_pct_style(ticker.change_pct)),
            )
        vix = next((ticker for ticker in board.tickers if ticker.symbol == "VIX"), None)
        detail = Text()
        if vix is not None:
            detail.append("VIX regime ", style="grey70")
            detail.append(_vix_regime(vix.value), style=_vix_regime_style(vix.value))
            detail.append("  ", style="grey70")
        detail.append(board.note, style="grey62")
        return Panel(Group(table, detail), title="[bold bright_white]Risk Context[/bold bright_white]", border_style="magenta", box=box.SQUARE)

    def _render_lens(self, state: dict):
        focus = state["top_focus"]
        tape = Table(box=box.MINIMAL_HEAVY_HEAD, expand=True, title="Market Tape")
        tape.add_column("City", style="bright_cyan", width=6)
        tape.add_column("Bucket")
        tape.add_column("Spark")
        tape.add_column("Last", justify="right")
        for row in state["market_tape"][:6]:
            tape.add_row(row["city"], row["bucket"], row["spark"], row["last"])
        if not state["market_tape"]:
            tape.add_row("-", "-", "-", "-")

        if focus:
            detail = Table.grid(expand=True)
            detail.add_column(style="bright_cyan", width=9)
            detail.add_column(style="bold bright_white")
            detail.add_row("City", focus.city)
            detail.add_row("Bucket", focus.bucket)
            detail.add_row("Bias", Text(focus.side, style=_side_style(focus.side)))
            detail.add_row("Market", _fmt_prob(focus.market_prob))
            detail.add_row("Model", _fmt_prob(focus.model_prob))
            detail.add_row("Edge", f"{focus.edge:.3f}")
            detail.add_row("Threshold", f"{focus.threshold:.3f}")
            detail.add_row("Mode", focus.kind.upper())
            lens_panel = Panel(detail, title="[bold bright_white]Focus Lens[/bold bright_white]", border_style="bright_green" if focus.kind == "signal" else "yellow", box=box.SQUARE)
        else:
            standby = Text()
            standby.append("No active or near-threshold setups.\n", style="bold yellow")
            standby.append("The blotter is clean and the radar is waiting for better edges.", style="grey70")
            lens_panel = Panel(standby, title="[bold bright_white]Focus Lens[/bold bright_white]", border_style="yellow", box=box.SQUARE)

        return Group(lens_panel, Panel(tape, border_style="bright_blue", box=box.SQUARE))

    def _render_orders(self, state: dict):
        table = Table(box=box.MINIMAL_HEAVY_HEAD, expand=True, title="Open Orders")
        table.add_column("Venue", width=10)
        table.add_column("City", width=6, style="bright_cyan")
        table.add_column("Bucket", min_width=12)
        table.add_column("Side", width=5)
        table.add_column("Px", justify="right")
        table.add_column("Qty", justify="right")
        table.add_column("Age", justify="right")
        if state["open_orders"]:
            now = datetime.now(UTC)
            for row in state["open_orders"][:10]:
                created = datetime.fromisoformat(row["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                age_minutes = int((now - created.astimezone(UTC)).total_seconds() // 60)
                table.add_row(
                    row["venue"],
                    row["city"],
                    str(row["bucket_label"])[:18],
                    Text(row["side"].upper(), style=_side_style(row["side"].upper())),
                    f"{float(row['price']):.3f}",
                    str(row["contracts"]),
                    f"{age_minutes}m",
                )
        else:
            table.add_row("-", "-", "Order blotter empty", "-", "-", "-", "-")
        return Panel(table, border_style="bright_blue", box=box.SQUARE)

    def _render_fills(self, state: dict):
        table = Table(box=box.MINIMAL_HEAVY_HEAD, expand=True, title="Recent Fills")
        table.add_column("Venue", width=10)
        table.add_column("Side", width=5)
        table.add_column("Qty", justify="right")
        table.add_column("Px", justify="right")
        table.add_column("Fee", justify="right")
        if state["recent_fills"]:
            for row in state["recent_fills"][:10]:
                table.add_row(
                    row["venue"],
                    Text(row["side"].upper(), style=_side_style(row["side"].upper())),
                    str(row["contracts"]),
                    f"{float(row['price']):.3f}",
                    _fmt_money(float(row["fee"])),
                )
        else:
            table.add_row("-", "-", "-", "-", "-")
        return Panel(table, border_style="bright_green", box=box.SQUARE)

    def _render_alerts(self, state: dict):
        table = Table(box=box.MINIMAL_HEAVY_HEAD, expand=True, title="Alerts / Logs")
        table.add_column("Lvl", width=8)
        table.add_column("Time", width=8)
        table.add_column("Message", overflow="fold")
        if state["recent_logs"]:
            for row in state["recent_logs"][:10]:
                level_style = _log_style(row["level"])
                ts = _fmt_clock(row["ts"])
                table.add_row(Text(str(row["level"])[:8], style=level_style), ts, str(row["msg"])[:100])
        else:
            table.add_row("-", "-", "No warnings or errors logged")
        return Panel(table, border_style="bright_red", box=box.SQUARE)

    def _load_model(self, city: str) -> Optional[ModelOutput]:
        rows = self.q(
            "SELECT data_json FROM weather_cache WHERE city=? AND data_type='model'",
            (city,),
        )
        if not rows:
            return None
        data = json.loads(rows[0]["data_json"])
        return ModelOutput(
            city=city,
            settlement_date=str(data.get("settlement_date", city_local_now(city).date().isoformat())),
            expected_high=float(data.get("expected_high", 0)),
            std_dev=float(data.get("std_dev", 0)),
            bucket_probs=data.get("bucket_probs", {}),
            confidence=float(data.get("confidence", 0)),
            obs_temp=_to_float(data.get("obs_temp")),
            forecast_temp=_to_float(data.get("forecast_temp")),
            observed_high_so_far=_to_float(data.get("observed_high_so_far")),
            remaining_forecast_high=_to_float(data.get("remaining_forecast_high")),
            local_hour=data.get("local_hour"),
        )

    def _load_latest_open_snapshots(self, city: str) -> list[MarketSnapshot]:
        rows = self.q(
            """
            SELECT ms.*
            FROM market_snapshots ms
            INNER JOIN (
                SELECT market_id, venue, MAX(ts) AS max_ts
                FROM market_snapshots
                WHERE city=? AND is_open=1
                GROUP BY market_id, venue
            ) latest
              ON latest.market_id = ms.market_id
             AND latest.venue = ms.venue
             AND latest.max_ts = ms.ts
            ORDER BY ms.bucket_min
            """,
            (city,),
        )
        return [_row_to_snapshot(row) for row in rows]

    def _analyze_city(self, city: str, model: Optional[ModelOutput], snapshots: list[MarketSnapshot]) -> dict:
        signals: list[RadarRow] = []
        watchlist: list[RadarRow] = []
        entry_open = can_open_new_same_day_positions(city)

        if not model:
            return {"signals": signals, "watchlist": watchlist, "focus_row": None}

        for snapshot in snapshots:
            analysis = analyze_market(snapshot, model)
            if analysis is None or analysis.best_side is None or analysis.best_market_prob is None or analysis.best_side_fair is None:
                continue

            row = RadarRow(
                city=city,
                bucket=str(snapshot.bucket_label),
                side=analysis.best_side.value.upper(),
                market_prob=analysis.best_market_prob,
                model_prob=analysis.best_side_fair,
                edge=analysis.best_edge,
                threshold=analysis.threshold,
                size=0,
                local_time=city_local_now(city).strftime("%H:%M"),
                reason=" ".join(analysis.guardrail_notes) if analysis.guardrail_notes else "watch",
                kind="watch",
            )

            if entry_open:
                signal = compute_edge(snapshot, model)
                if signal is not None:
                    sized = size_signal(signal, snapshot, model, self.sizing_db)
                    if sized is not None:
                        row.size = sized.suggested_contracts
                        row.kind = "signal"
                        row.reason = sized.reason
                        signals.append(row)
                        continue

            if row.edge > 0:
                watchlist.append(row)

        focus_candidates = signals or watchlist
        focus_row = focus_candidates[0] if focus_candidates else None
        if signals:
            signals.sort(key=lambda item: item.edge, reverse=True)
        if watchlist:
            watchlist.sort(key=lambda item: (item.gap, -item.edge))

        return {"signals": signals, "watchlist": watchlist, "focus_row": focus_row}

    def _build_city_view(
        self,
        city: str,
        display_name: str,
        model: Optional[ModelOutput],
        snapshots: list[MarketSnapshot],
        city_rows: dict,
    ) -> CityView:
        focus_row = city_rows["focus_row"]
        focus_snapshot = next((snap for snap in snapshots if focus_row and snap.bucket_label == focus_row.bucket), snapshots[0] if snapshots else None)
        spark = self._spark_for_market(focus_snapshot.market_id) if focus_snapshot else ""
        entry_open = can_open_new_same_day_positions(city)
        border_style = "bright_green" if city_rows["signals"] else "yellow" if city_rows["watchlist"] else ("bright_blue" if entry_open else "red")
        subtitle = display_name
        if model:
            subtitle = f"obs {model.obs_temp if model.obs_temp is not None else '-'} | hi {model.observed_high_so_far if model.observed_high_so_far is not None else '-'} | conf {model.confidence:.0%}"

        return CityView(
            city=city,
            display_name=display_name,
            local_time=city_local_now(city).strftime("%H:%M"),
            entry_open=entry_open,
            expected_high=model.expected_high if model else None,
            confidence=model.confidence if model else None,
            obs_temp=model.obs_temp if model else None,
            observed_high=model.observed_high_so_far if model else None,
            forecast_high=model.forecast_temp if model else None,
            remaining_forecast_high=model.remaining_forecast_high if model else None,
            focus_bucket=focus_row.bucket if focus_row else (str(focus_snapshot.bucket_label) if focus_snapshot else "No market"),
            focus_side=focus_row.side if focus_row else ("-" if not focus_snapshot else "WATCH"),
            focus_edge=focus_row.edge if focus_row else None,
            focus_market=focus_row.market_prob if focus_row else None,
            focus_model=focus_row.model_prob if focus_row else None,
            spark=spark,
            signal_count=len(city_rows["signals"]),
            watch_count=len(city_rows["watchlist"]),
            monitored_markets=len(snapshots),
            border_style=border_style,
            subtitle=subtitle,
        )

    def _city_panel(self, city: CityView):
        title = Text()
        title.append(f" {city.city} ", style="bold black on bright_cyan")
        title.append(f" {city.local_time} ", style="bold bright_white")
        title.append(" ENTRY OPEN ", style="bold black on green" if city.entry_open else "bold white on red")

        body = Table.grid(expand=True)
        body.add_column(style="bright_white")
        body.add_column(justify="right", style="bold bright_white")
        body.add_row("Expected", _fmt_temp(city.expected_high))
        body.add_row("Observed", _fmt_temp(city.obs_temp))
        body.add_row("High So Far", _fmt_temp(city.observed_high))
        body.add_row("Forecast Hi", _fmt_temp(city.forecast_high))
        body.add_row("Focus", city.focus_bucket[:18])
        body.add_row("Bias", Text(city.focus_side, style=_side_style(city.focus_side)))
        body.add_row("Edge", f"{city.focus_edge:.3f}" if city.focus_edge is not None else "-")
        body.add_row("Mkt / Model", f"{_fmt_prob(city.focus_market)} / {_fmt_prob(city.focus_model)}" if city.focus_market is not None and city.focus_model is not None else "-")
        body.add_row("Tape", city.spark or "-")
        body.add_row("Signals", f"{city.signal_count} sig / {city.watch_count} watch")

        return Panel(body, title=title, subtitle=city.subtitle, border_style=city.border_style, box=box.SQUARE)

    def _portfolio_snapshot(self, balances: BalanceSnapshot, positions: list[dict]) -> dict:
        cash_available = balances.cash_available
        position_rows: list[dict] = []
        unrealized = 0.0
        positions_value = 0.0
        history_segments: list[list[float]] = []

        for position in positions:
            history = self.q(
                """
                SELECT COALESCE(last_price, (CAST(best_bid AS REAL) + CAST(best_ask AS REAL)) / 2.0, best_bid, best_ask) AS px
                FROM market_snapshots
                WHERE market_id=? AND COALESCE(last_price, best_bid, best_ask) IS NOT NULL
                ORDER BY ts DESC
                LIMIT 30
                """,
                (position["market_id"],),
            )
            if not history:
                continue
            probs = [float(row["px"]) for row in reversed(history) if row["px"] is not None]
            if not probs:
                continue
            side = str(position["side"]).upper()
            side_probs = probs if side == "YES" else [max(0.0, min(1.0, 1 - prob)) for prob in probs]
            avg_price = float(position["avg_price"])
            contracts = int(position["contracts"])
            mark_prob = side_probs[-1]
            value = mark_prob * contracts
            pnl = (mark_prob - avg_price) * contracts
            positions_value += value
            unrealized += pnl
            history_segments.append([prob * contracts for prob in side_probs])
            position_rows.append(
                {
                    "city": position["city"],
                    "bucket": position["bucket_label"],
                    "side": side,
                    "mark_prob": mark_prob,
                    "pnl": pnl,
                }
            )

        target_len = max((len(segment) for segment in history_segments), default=24)
        if history_segments:
            padded = [_pad_series(segment, target_len) for segment in history_segments]
            mark_curve = [sum(series[idx] for series in padded) for idx in range(target_len)]
        else:
            mark_curve = [positions_value] * target_len

        account_value = balances.account_value
        if account_value is None:
            base_cash = cash_available if cash_available is not None else 0.0
            account_value = base_cash + positions_value
        baseline_cash = account_value - positions_value
        curve = [baseline_cash + point for point in mark_curve] if mark_curve else [account_value] * 24
        daily_pnl = curve[-1] - curve[0] if curve else 0.0

        position_rows.sort(key=lambda row: row["pnl"], reverse=True)
        return {
            "cash_available": cash_available if cash_available is not None else baseline_cash,
            "account_value": account_value,
            "positions_value": positions_value,
            "unrealized_pnl": unrealized,
            "daily_pnl": daily_pnl,
            "curve": curve or [account_value] * 24,
            "position_rows": position_rows,
        }

    def _market_tape_row(self, city: str, snapshots: list[MarketSnapshot]) -> Optional[dict]:
        if not snapshots:
            return None
        snapshot = max(snapshots, key=lambda snap: _mid_prob(snap) or 0.0)
        spark = self._spark_for_market(snapshot.market_id)
        last = _mid_prob(snapshot)
        return {
            "city": city,
            "bucket": str(snapshot.bucket_label)[:18],
            "spark": spark or "-",
            "last": _fmt_prob(last) if last is not None else "-",
        }

    def _spark_for_market(self, market_id: str) -> str:
        history = self.q(
            """
            SELECT COALESCE(last_price, (CAST(best_bid AS REAL) + CAST(best_ask AS REAL)) / 2.0, best_bid, best_ask) AS px
            FROM market_snapshots
            WHERE market_id=? AND COALESCE(last_price, best_bid, best_ask) IS NOT NULL
            ORDER BY ts DESC
            LIMIT 24
            """,
            (market_id,),
        )
        values = [float(row["px"]) for row in reversed(history) if row["px"] is not None]
        return _sparkline(values)

    def _context_board(self) -> MacroBoard:
        now = time.time()
        if now < self.context_cache[0]:
            return self.context_cache[1]

        board = self._fetch_macro_board()
        self.context_cache = (now + 300, board)
        return board

    def _fetch_macro_board(self) -> MacroBoard:
        symbols = {
            "^VIX": ("VIX", "fear"),
            "SPY": ("SPY", "equity"),
            "USO": ("USO", "oil"),
            "UNG": ("UNG", "natgas"),
        }
        url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=%5EVIX,SPY,USO,UNG"
        request = urllib.request.Request(url, headers={"User-Agent": "weather-trader-dashboard/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=2.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            results = payload["quoteResponse"]["result"]
            rows: list[MacroTicker] = []
            lookup = {item.get("symbol"): item for item in results}
            for raw_symbol, (symbol, theme) in symbols.items():
                item = lookup.get(raw_symbol)
                value = _to_float(item.get("regularMarketPrice")) if item else None
                change_pct = _to_float(item.get("regularMarketChangePercent")) if item else None
                name = str(item.get("shortName") or item.get("longName") or symbol) if item else symbol
                if value is None or change_pct is None:
                    raise ValueError(f"missing quote for {symbol}")
                rows.append(
                    MacroTicker(
                        symbol=symbol,
                        name=name,
                        value=round(value, 2),
                        change_pct=round(change_pct, 2),
                        theme=theme,
                    )
                )
            return MacroBoard(tickers=rows, note="live yahoo quote feed")
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError, json.JSONDecodeError):
            return self._fallback_macro_board("fallback cached macro tape")

    def _fallback_macro_board(self, note: str) -> MacroBoard:
        return MacroBoard(
            tickers=[
                MacroTicker(symbol="VIX", name="CBOE Volatility Index", value=18.42, change_pct=-1.28, theme="fear"),
                MacroTicker(symbol="SPY", name="SPDR S&P 500 ETF", value=512.37, change_pct=0.44, theme="equity"),
                MacroTicker(symbol="USO", name="United States Oil Fund", value=79.31, change_pct=-0.62, theme="oil"),
                MacroTicker(symbol="UNG", name="United States Natural Gas Fund", value=14.86, change_pct=1.17, theme="natgas"),
            ],
            note=note,
        )

    def _session_pulse(
        self,
        city_views: list[CityView],
        signals: list[RadarRow],
        watchlist: list[RadarRow],
        recent_logs: list[dict],
    ) -> SessionPulse:
        confidences = [city.confidence for city in city_views if city.confidence is not None]
        observed_coverage = sum(1 for city in city_views if city.observed_high is not None)
        hottest_city = None
        hottest_temp = None
        for city in city_views:
            if city.expected_high is None:
                continue
            if hottest_temp is None or city.expected_high > hottest_temp:
                hottest_city = city.city
                hottest_temp = city.expected_high
        return SessionPulse(
            open_cities=sum(1 for city in city_views if city.entry_open),
            closed_cities=sum(1 for city in city_views if not city.entry_open),
            avg_confidence=sum(confidences) / len(confidences) if confidences else None,
            best_edge=max((row.edge for row in signals), default=None),
            watch_gap=min((row.gap for row in watchlist), default=None),
            hottest_city=hottest_city,
            hottest_temp=hottest_temp,
            observed_coverage=observed_coverage,
            expected_coverage=len(city_views),
            guardrail_alerts=sum(1 for row in recent_logs if str(row["level"]).upper() in {"WARNING", "ERROR", "CRITICAL"}),
            signals_live=len(signals),
            watch_live=len(watchlist),
        )

    def _upcoming_windows(self) -> list[dict]:
        rows = []
        for meta in active_cities():
            local_now = city_local_now(meta.city_code)
            if can_open_new_same_day_positions(meta.city_code):
                state = "window open"
                clock = f"closes in {hours_until_entry_cutoff(meta.city_code):.1f}h"
            else:
                until_start = hours_until_entry_start(meta.city_code)
                if until_start > 0:
                    state = "pre-open"
                    clock = f"opens in {until_start:.1f}h"
                else:
                    next_open = local_now + timedelta(days=1)
                    state = "next session"
                    clock = next_open.strftime("%a %H:%M")
            rows.append(
                {
                    "city": meta.city_code,
                    "local": local_now.strftime("%H:%M"),
                    "state": state,
                    "clock": clock,
                }
            )
        rows.sort(key=lambda row: (0 if row["state"] == "window open" else 1, row["city"]))
        return rows

    def _system_status(self) -> dict[str, str]:
        rows = self.q("SELECT MAX(ts) AS last_snap FROM market_snapshots")
        market_age = "no data"
        if rows and rows[0]["last_snap"]:
            dt = datetime.fromisoformat(rows[0]["last_snap"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            age = int((datetime.now(UTC) - dt.astimezone(UTC)).total_seconds())
            market_age = f"{age}s"
        return {
            "live_mode": "LIVE" if cfg.live_enabled else "PAPER",
            "kill": "ON" if cfg.kill_switch else "OFF",
            "safe": safe_mode_reason() if is_safe_mode() else "normal",
            "market_age": market_age,
        }

    def _fills_today_count(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        rows = self.q("SELECT COUNT(*) AS c FROM fills WHERE substr(ts, 1, 10)=?", (today,))
        return int(rows[0]["c"]) if rows else 0

    def _total_open_exposure(self) -> float:
        total = Decimal("0")
        for venue in Venue:
            total += self.sizing_db.get_total_exposure(venue)
        return float(total)

    def _get_balances(self) -> BalanceSnapshot:
        now = time.time()
        if now < self.balance_cache[0]:
            return self.balance_cache[1]

        snapshot = BalanceSnapshot()
        try:
            if cfg.has_kalshi_creds():
                from venues.kalshi import KalshiClient

                kalshi = KalshiClient()
                balance = kalshi.get_balance()
                snapshot.kalshi_available = float(balance.available)
                snapshot.kalshi_total = float(balance.total)
                kalshi.close()
        except Exception:
            pass

        try:
            if cfg.has_poly_creds():
                from venues.polymarket import PolymarketClient

                poly = PolymarketClient()
                if poly.active:
                    balance = poly.get_balance()
                    snapshot.poly_available = float(balance.available)
                    snapshot.poly_total = float(balance.total)
        except Exception:
            pass

        self.balance_cache = (now + 30, snapshot)
        return snapshot


def _metric_card(title: str, value: str, subtitle: str, color: str) -> Panel:
    value_text = Text(value, style=f"bold {color}")
    subtitle_text = Text(subtitle, style="grey70")
    return Panel(
        Group(Text(title, style="bold bright_white"), value_text, subtitle_text),
        border_style=color,
        box=box.SQUARE,
    )


def _fmt_money(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"${value:,.2f}"


def _fmt_signed_money(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:+.2f}"


def _fmt_prob(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:.1%}"


def _fmt_temp(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}F"


def _fmt_clock(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(UTC).strftime("%H:%M")
    except Exception:
        return "--:--"


def _same_day_utc(value: str) -> bool:
    try:
        return datetime.fromisoformat(value).astimezone(UTC).date() == datetime.now(UTC).date()
    except Exception:
        return False


def _pnl_color(value: Optional[float]) -> str:
    if value is None:
        return "bright_white"
    if value > 0:
        return "green"
    if value < 0:
        return "red"
    return "yellow"


def _pct_style(value: Optional[float]) -> str:
    if value is None:
        return "bright_white"
    if value > 0:
        return "bold green"
    if value < 0:
        return "bold red"
    return "bold yellow"


def _side_style(side: str) -> str:
    return "bold green" if side.upper() == "YES" else "bold red" if side.upper() == "NO" else "bold yellow"


def _log_style(level: str) -> str:
    level = str(level).upper()
    if level in {"ERROR", "CRITICAL"}:
        return "bold red"
    if level == "WARNING":
        return "bold yellow"
    return "bright_white"


def _mode_style(mode: str) -> str:
    return "bold white on red" if mode == "LIVE" else "bold black on yellow"


def _vix_regime(value: float) -> str:
    if value >= 30:
        return "stressed"
    if value >= 20:
        return "elevated"
    if value >= 15:
        return "watch"
    return "calm"


def _vix_regime_style(value: float) -> str:
    if value >= 30:
        return "bold white on red"
    if value >= 20:
        return "bold black on yellow"
    if value >= 15:
        return "bold black on bright_magenta"
    return "bold black on green"


def _mid_prob(snapshot: MarketSnapshot) -> Optional[float]:
    if snapshot.last_price is not None:
        return float(snapshot.last_price)
    if snapshot.best_bid is not None and snapshot.best_ask is not None:
        return float((snapshot.best_bid + snapshot.best_ask) / 2)
    if snapshot.best_bid is not None:
        return float(snapshot.best_bid)
    if snapshot.best_ask is not None:
        return float(snapshot.best_ask)
    return None


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return SPARK_CHARS[0] * len(values)
    result = []
    for value in values:
        idx = int((value - lo) / (hi - lo) * (len(SPARK_CHARS) - 1))
        idx = max(0, min(idx, len(SPARK_CHARS) - 1))
        result.append(SPARK_CHARS[idx])
    return "".join(result)


def _pad_series(values: list[float], size: int) -> list[float]:
    if len(values) >= size:
        return values[-size:]
    pad = [values[0]] * (size - len(values))
    return pad + values


def _to_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    return float(value)


def _row_to_snapshot(row: dict) -> MarketSnapshot:
    ts = datetime.fromisoformat(row["ts"]) if row.get("ts") else datetime.now(UTC)
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
        best_bid=Decimal(str(row["best_bid"])) if row["best_bid"] not in (None, "") else None,
        best_ask=Decimal(str(row["best_ask"])) if row["best_ask"] not in (None, "") else None,
        last_price=Decimal(str(row["last_price"])) if row["last_price"] not in (None, "") else None,
        yes_bid=None,
        yes_ask=None,
        ts=ts,
        is_open=bool(row["is_open"]),
    )


def main():
    parser = argparse.ArgumentParser(description="Weather Trader terminal dashboard")
    parser.add_argument("--refresh", type=float, default=2.0, help="Refresh interval in seconds")
    parser.add_argument("--once", action="store_true", help="Render a single frame and exit")
    args = parser.parse_args()

    dashboard = TerminalDashboard(DB_PATH)
    try:
        if args.once:
            dashboard.console.print(dashboard.render())
            return
        refresh_per_second = max(1, int(round(1 / max(args.refresh, 0.25))))
        with Live(dashboard.render(), console=dashboard.console, refresh_per_second=refresh_per_second, screen=True) as live:
            while True:
                live.update(dashboard.render())
                time.sleep(args.refresh)
    except KeyboardInterrupt:
        pass
    finally:
        dashboard.close()


if __name__ == "__main__":
    main()
