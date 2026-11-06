"""
ChronosMatch: Ultra-High-Performance Real-Time Terminal Trading Cockpit.
Institutional-grade Terminal User Interface (TUI) built on Rich.

Visual Capabilities:
1. Header & Telemetry Bar: Live pulsing heartbeat, architecture telemetry (Zero-Copy IPC, nogil LOB, SQLite WAL).
2. KPI Analytics Deck: Real-time throughput velocity, Top-of-Book spread & imbalance gauge, SLA latency percentiles, and liquidity volume.
3. Level 2 Market Depth Ladder: Dual-sided order book with cumulative volume depth bars and spread compression ribbon.
4. Live Time & Sales Tape: Execution stream with tick directions (▲/▼), notional turnover, and sub-microsecond latency profiling.
5. Institutional Whale Radar & Anomaly Log: Sweep anomaly classification (Whale Sweep, Block Trade, SLA Breaches).
6. Microstructure Sparklines: Real-time rolling Unicode sparklines for price action, latency jitter, and order ingress velocity.
7. Standalone Interactive Simulator: Run directly via `python -m chronos_match.monitor.dashboard`.
"""

from collections import deque
from datetime import datetime
import io
import math
import random
import sys
import time
from typing import Any, Deque, Dict, List, Optional, Tuple

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chronos_match.protocol import from_fixed_price, to_fixed_price, Side
from chronos_match.monitor.metrics import LatencyStats
from chronos_match.monitor.whale_detector import WhaleAlert


# Unicode sparkline glyphs ordered from lowest to highest
SPARK_CHARS = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
PULSE_FRAMES = ["●", "◉", "◎", "○", "◎", "◉"]
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def render_sparkline(
    values: List[float],
    width: int = 12,
    min_val: Optional[float] = None,
    max_val: Optional[float] = None,
) -> str:
    """
    Render a clean Unicode sparkline from numerical series.
    Normalizes across series range and maps each element to 8 discrete vertical levels.
    """
    if not values:
        return "─" * width

    # Slice to width
    series = values[-width:] if len(values) > width else values
    lo = min_val if min_val is not None else min(series)
    hi = max_val if max_val is not None else max(series)

    if math.isclose(hi, lo, abs_tol=1e-9):
        return SPARK_CHARS[3] * len(series)

    span = hi - lo
    n_levels = len(SPARK_CHARS)
    result = []
    for v in series:
        ratio = (v - lo) / span
        idx = int(ratio * (n_levels - 1))
        idx = max(0, min(n_levels - 1, idx))
        result.append(SPARK_CHARS[idx])

    return "".join(result)


class TerminalDashboard:
    """
    Real-Time Terminal User Interface (TUI) Dashboard for ChronosMatch.
    Engineered with high-density institutional Bloomberg/HFT cyber-trading desk aesthetics.
    """

    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console()
        self._frame_count: int = 0
        self._start_time: float = time.perf_counter()

        # Rolling time series for visual sparklines & trends
        self._tps_history: Deque[float] = deque(maxlen=24)
        self._latency_history: Deque[float] = deque(maxlen=24)
        self._price_history: Deque[float] = deque(maxlen=24)
        self._last_trade_price: Optional[float] = None
        self._last_trade_id: int = 0
        self._tick_direction: str = "FLAT"  # "UP", "DOWN", "FLAT"

    def build_layout(
        self,
        l2_snapshot: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        latency_stats: Optional[LatencyStats] = None,
        throughput_info: Optional[Dict[str, Any]] = None,
        recent_trades: Optional[List[Dict[str, Any]]] = None,
        whale_alerts: Optional[List[WhaleAlert]] = None,
        hotkey_banner: Optional[str] = None,
    ) -> Layout:
        """
        Construct a responsive Rich Layout hierarchy from current engine snapshots.
        """
        self._frame_count += 1

        # Update internal rolling metrics
        info = throughput_info or {}
        rate = float(info.get("orders_per_sec", 0.0))
        if rate > 0:
            self._tps_history.append(rate)

        if latency_stats and latency_stats.sample_count > 0:
            p50_us = getattr(latency_stats, "p50_us", getattr(latency_stats, "p50_ns", 0) / 1000.0)
            self._latency_history.append(p50_us)

        # Track tick direction from trades
        trades = recent_trades or []
        if trades:
            latest = trades[-1]
            raw_p = latest.get("price", latest.get("execution_price", 0))
            p_float = from_fixed_price(raw_p) if raw_p > 100_000 else float(raw_p)
            if self._last_trade_price is not None:
                if p_float > self._last_trade_price:
                    self._tick_direction = "UP"
                elif p_float < self._last_trade_price:
                    self._tick_direction = "DOWN"
            self._last_trade_price = p_float
            self._price_history.append(p_float)

        layout = Layout()

        # Multi-row layout hierarchy with footer hotkeys bar
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="kpis", size=6),
            Layout(name="main_body", ratio=2),
            Layout(name="alerts", size=8),
            Layout(name="footer", size=3),
        )

        layout["main_body"].split_row(
            Layout(name="order_book", ratio=1),
            Layout(name="trade_ticker", ratio=1),
        )

        # 1. Header Panel
        layout["header"].update(self._build_header())

        # 2. KPI Cards Panel
        layout["kpis"].update(
            self._build_kpi_cards(throughput_info, latency_stats, l2_snapshot, recent_trades)
        )

        # 3. L2 Order Book Depth Table
        layout["order_book"].update(self._build_order_book_panel(l2_snapshot))

        # 4. Live Trade Execution Ticker Table
        layout["trade_ticker"].update(self._build_trade_ticker_panel(recent_trades))

        # 5. Whale & SLA Breach Alerts Panel
        layout["alerts"].update(self._build_alerts_panel(whale_alerts))

        # 6. Interactive Hotkey Footer
        layout["footer"].update(self._build_footer(hotkey_banner))

        return layout

    def _build_footer(self, hotkey_banner: Optional[str] = None) -> Panel:
        """Render interactive command hotkeys footer bar."""
        text = Text()
        text.append(" HOTKEYS: ", style="bold bright_black on #1a1f2c")
        text.append(" [B] ", style="bold black on #00ffaa")
        text.append("Whale Buy  ", style="bold white")
        text.append("[S] ", style="bold white on #ff3366")
        text.append("Whale Sell  ", style="bold white")
        text.append("[C] ", style="bold black on #ffaa00")
        text.append("Cancel Storm  ", style="bold white")
        text.append("[R] ", style="bold black on #00e5ff")
        text.append("Rate Cycle  ", style="bold white")
        text.append("[Space] ", style="bold white on #6c5ce7")
        text.append("Pause/Resume  ", style="bold white")
        text.append("[Q] ", style="bold white on #d63031")
        text.append("Quit", style="bold white")

        if hotkey_banner:
            text.append(f" │ {hotkey_banner}", style="bold bright_yellow")
        else:
            text.append(" │ Live Interactive Stream Active", style="dim green")

        return Panel(
            text,
            style="bold white on #080d14",
            border_style="bright_blue",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _build_header(self) -> Panel:
        """Generate high-contrast, institutional cyber-styled header banner."""
        pulse_glyph = PULSE_FRAMES[self._frame_count % len(PULSE_FRAMES)]
        spinner_glyph = SPINNER_FRAMES[self._frame_count % len(SPINNER_FRAMES)]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        title = Text()
        title.append("⚡ CHRONOS MATCH ", style="bold bright_cyan")
        title.append("│ ZERO-COPY LOB │ ", style="bold white")
        title.append("PURE-C NOGIL • SHM SPSC IPC • SQLITE WAL ", style="bold green")
        title.append(f"│ {now_str} ", style="bright_yellow")
        title.append(f"{pulse_glyph} NOMINAL", style="bold bright_green")
        title.append(f" [{spinner_glyph}]", style="bold cyan")

        return Panel(
            title,
            style="bold white on #080d14",
            border_style="bright_blue",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _build_kpi_cards(
        self,
        throughput_info: Optional[Dict[str, Any]],
        latency_stats: Optional[LatencyStats],
        l2_snapshot: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        recent_trades: Optional[List[Dict[str, Any]]] = None,
    ) -> Table:
        """Generate top-row KPI metric cards with microsecond telemetry and sparklines."""
        info = throughput_info or {}
        orders_proc = info.get("orders_processed", 0)
        cancels_proc = info.get("cancellations_processed", 0)
        trades_gen = info.get("trades_generated", 0)
        shares_traded = info.get("shares_traded", 0)
        rate = float(info.get("orders_per_sec", 0.0))

        best_bid = from_fixed_price(info.get("best_bid", 0))
        best_ask = from_fixed_price(info.get("best_ask", 0))
        spread = from_fixed_price(info.get("spread", 0))

        # Depth volumes for Order Flow Imbalance (OFI)
        bids = (l2_snapshot or {}).get("bids", [])
        asks = (l2_snapshot or {}).get("asks", [])
        tot_bid_vol = sum(b.get("volume", 0) for b in bids)
        tot_ask_vol = sum(a.get("volume", 0) for a in asks)
        tot_vol = tot_bid_vol + tot_ask_vol

        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)

        # -------------------------------------------------------------
        # CARD 1: Throughput Velocity & Ingestion
        # -------------------------------------------------------------
        tps_spark = render_sparkline(list(self._tps_history), width=8)
        c1 = Text()
        c1.append("Throughput & Ingestion ", style="bold bright_cyan")
        if tps_spark:
            c1.append(f"[{tps_spark}]\n", style="dim cyan")
        else:
            c1.append("\n")
        c1.append(f"Orders: {orders_proc:,} ", style="bold white")
        c1.append(f"({cancels_proc:,} can)\n", style="dim")
        c1.append(f"Rate: {rate:,.0f} ord/s", style="bold bright_green" if rate > 0 else "dim")

        # -------------------------------------------------------------
        # CARD 2: Market Top-of-Book & Imbalance
        # -------------------------------------------------------------
        c2 = Text()
        c2.append("Market Top-of-Book\n", style="bold bright_cyan")
        if best_bid > 0 and best_ask > 0:
            bps = (spread / best_bid) * 10000 if best_bid > 0 else 0
            c2.append(f"Bid: ${best_bid:,.2f}  ", style="bold green")
            c2.append(f"Ask: ${best_ask:,.2f}\n", style="bold red")
            c2.append(f"Spread: ${spread:,.2f} ({bps:.1f} bps)", style="bold yellow")
            if tot_vol > 0:
                bid_pct = int((tot_bid_vol / tot_vol) * 100)
                ask_pct = 100 - bid_pct
                bar_bid_len = max(1, int((bid_pct / 100) * 10))
                bar_ask_len = max(1, 10 - bar_bid_len)
                c2.append("\n")
                c2.append("█" * bar_bid_len, style="bold green")
                c2.append("█" * bar_ask_len, style="bold red")
                c2.append(f" {bid_pct}%B / {ask_pct}%A", style="dim cyan")
        else:
            c2.append("Book Empty\nWaiting for Orders", style="dim")

        # -------------------------------------------------------------
        # CARD 3: Wire-to-Match Latency SLA
        # -------------------------------------------------------------
        lat_spark = render_sparkline(list(self._latency_history), width=8)
        c3 = Text()
        c3.append("Wire-to-Match Latency ", style="bold bright_cyan")
        if lat_spark:
            c3.append(f"[{lat_spark}]\n", style="dim green")
        else:
            c3.append("\n")
        if latency_stats and latency_stats.sample_count > 0:
            p50_us = getattr(latency_stats, "p50_us", getattr(latency_stats, "p50_ns", 0) / 1000.0)
            p99_us = getattr(latency_stats, "p99_us", getattr(latency_stats, "p99_ns", 0) / 1000.0)
            sla_ok = p99_us <= 50.0
            c3.append(f"p50: {p50_us:.2f}μs  ", style="bold green")
            c3.append(f"p99: {p99_us:.2f}μs\n", style="bold green" if sla_ok else "bold red")
            sla_badge = "SLA TARGET MET (< 50μs)" if sla_ok else "SLA TARGET EXCEEDED"
            c3.append(sla_badge, style="bold green" if sla_ok else "bold red")
        else:
            c3.append("Awaiting Latency Samples\n--", style="dim")

        # -------------------------------------------------------------
        # CARD 4: Matched Liquidity & Turnover
        # -------------------------------------------------------------
        price_spark = render_sparkline(list(self._price_history), width=8)
        c4 = Text()
        c4.append("Matched Liquidity ", style="bold bright_cyan")
        if price_spark:
            c4.append(f"[{price_spark}]\n", style="dim yellow")
        else:
            c4.append("\n")
        c4.append(f"Trades: {trades_gen:,}\n", style="bold white")
        c4.append(f"Volume: {shares_traded:,} shares", style="bold yellow")

        grid.add_row(
            Panel(c1, border_style="cyan", box=box.ROUNDED, padding=(0, 1)),
            Panel(c2, border_style="bright_cyan", box=box.ROUNDED, padding=(0, 1)),
            Panel(c3, border_style="cyan", box=box.ROUNDED, padding=(0, 1)),
            Panel(c4, border_style="bright_cyan", box=box.ROUNDED, padding=(0, 1)),
        )
        return grid

    def _build_order_book_panel(
        self, l2_snapshot: Optional[Dict[str, List[Dict[str, Any]]]]
    ) -> Panel:
        """Render Level 2 Order Book Depth Ladder with cumulative volume depth bars."""
        table = Table(
            title="LEVEL 2 MARKET DEPTH LADDER",
            title_style="bold bright_cyan",
            expand=True,
            show_header=True,
            header_style="bold white on #111927",
            box=box.SIMPLE_HEAD,
            pad_edge=False,
            padding=(0, 0),
        )
        table.add_column("#", justify="right", style="dim", width=3)
        table.add_column("Bid Depth", justify="right", width=9)
        table.add_column("Bids", justify="right", style="bold green", width=7)
        table.add_column("Price", justify="center", style="bold white", width=9)
        table.add_column("Asks", justify="left", style="bold red", width=7)
        table.add_column("Ask Depth", justify="left", width=9)
        table.add_column("#", justify="left", style="dim", width=3)

        snapshot = l2_snapshot or {"bids": [], "asks": []}
        bids = snapshot.get("bids", [])
        asks = snapshot.get("asks", [])

        # Find max volume for proportional bar rendering
        max_vol = 1
        for b in bids:
            if b.get("volume", 0) > max_vol:
                max_vol = b["volume"]
        for a in asks:
            if a.get("volume", 0) > max_vol:
                max_vol = a["volume"]

        # Render Top Asks (in reverse order so lowest ask is near the spread)
        asks_to_show = list(reversed(asks[:10]))
        for ask in asks_to_show:
            p_float = from_fixed_price(ask["price"])
            vol = ask["volume"]
            cnt = ask.get("order_count", 1)
            bar_len = int((vol / max_vol) * 8) if max_vol > 0 else 0
            bar = "█" * max(1, bar_len)
            table.add_row(
                "",
                "",
                "",
                f"${p_float:,.2f}",
                f"{vol:,}",
                f"[bold red]{bar}[/]",
                str(cnt),
            )

        # Mid-market Spread Divider
        if bids and asks:
            best_bid_p = from_fixed_price(bids[0]["price"])
            best_ask_p = from_fixed_price(asks[0]["price"])
            spread_p = best_ask_p - best_bid_p
            bps = (spread_p / best_bid_p) * 10000 if best_bid_p > 0 else 0
            spread_str = f"── SPREAD: ${spread_p:,.2f} ({bps:.1f} bps) ──"
            table.add_row(
                "", "", "", f"[bold yellow]{spread_str}[/]", "", "", ""
            )
        elif not bids and not asks:
            table.add_row(
                "", "", "", "[dim]-- Empty Book --[/]", "", "", ""
            )

        # Render Top Bids (descending order)
        for bid in bids[:10]:
            p_float = from_fixed_price(bid["price"])
            vol = bid["volume"]
            cnt = bid.get("order_count", 1)
            bar_len = int((vol / max_vol) * 8) if max_vol > 0 else 0
            bar = "█" * max(1, bar_len)
            table.add_row(
                str(cnt),
                f"[bold green]{bar:>8}[/]",
                f"{vol:,}",
                f"${p_float:,.2f}",
                "",
                "",
                "",
            )

        return Panel(
            table,
            border_style="bright_blue",
            box=box.ROUNDED,
            title="[bold bright_cyan]📊 Level 2 Book[/]",
            title_align="left",
        )

    def _build_trade_ticker_panel(
        self, recent_trades: Optional[List[Dict[str, Any]]]
    ) -> Panel:
        """Render recent executed trade executions stream with tick directions."""
        table = Table(
            title="LIVE TRADE EXECUTION STREAM",
            title_style="bold bright_cyan",
            expand=True,
            show_header=True,
            header_style="bold white on #111927",
            box=box.SIMPLE_HEAD,
            pad_edge=False,
            padding=(0, 0),
        )
        table.add_column("Trade ID", justify="right", style="dim cyan", width=8)
        table.add_column("Side", justify="center", width=6)
        table.add_column("Price", justify="right", width=9)
        table.add_column("Qty", justify="right", width=6)
        table.add_column("Notional", justify="right", style="bold yellow", width=10)
        table.add_column("Latency", justify="right", width=9)

        trades = recent_trades or []
        for t in trades[:12]:
            trade_id = t.get("trade_id", 0)
            side_val = t.get("aggressor_side", 0)
            side_str = "BUY" if side_val == int(Side.BUY) else "SELL"
            side_badge = (
                "[bold black on #00ff88] BUY [/]"
                if side_str == "BUY"
                else "[bold white on #ff3366] SELL [/]"
            )

            raw_price = t.get("price", t.get("execution_price", 0))
            price_float = from_fixed_price(raw_price) if raw_price > 100_000 else float(raw_price)
            qty = t.get("quantity", 0)
            notional = price_float * qty

            latency_ns = t.get("latency_ns", 0)
            latency_us = latency_ns / 1000.0
            if latency_us <= 10.0:
                lat_style = "bold bright_green"
                glyph = "⚡"
            elif latency_us <= 50.0:
                lat_style = "green"
                glyph = ""
            else:
                lat_style = "bold red"
                glyph = "⚠"

            table.add_row(
                f"#{trade_id}",
                side_badge,
                f"${price_float:,.2f}",
                f"{qty:,}",
                f"${notional:,.2f}",
                f"[{lat_style}]{latency_us:.1f}μs {glyph}[/]",
            )

        if not trades:
            table.add_row("", "", "[dim]No executions yet[/]", "", "", "")

        return Panel(
            table,
            border_style="bright_blue",
            box=box.ROUNDED,
            title="[bold bright_cyan]⚡ Executions Tape[/]",
            title_align="left",
        )

    def _build_alerts_panel(
        self, whale_alerts: Optional[List[WhaleAlert]]
    ) -> Panel:
        """Render real-time institutional flow and SLA breach alerts."""
        table = Table(
            expand=True,
            show_header=True,
            header_style="bold yellow on #111927",
            box=box.SIMPLE_HEAD,
            pad_edge=False,
        )
        table.add_column("Alert #", justify="right", style="dim", width=7)
        table.add_column("Type", justify="center", width=14)
        table.add_column("Side", justify="center", width=6)
        table.add_column("Price", justify="right", width=10)
        table.add_column("Shares", justify="right", width=9)
        table.add_column("Notional USD", justify="right", style="bold yellow", width=13)
        table.add_column("Latency", justify="right", width=10)
        table.add_column("Event Details", justify="left")

        alerts = whale_alerts or []
        for a in alerts[-4:]:
            type_style = (
                "bold black on #ffaa00"
                if a.alert_type == "WHALE_SWEEP"
                else (
                    "bold black on #00e5ff"
                    if a.alert_type == "LARGE_BLOCK"
                    else "bold white on #ff0055"
                )
            )
            type_badge = f"[{type_style}] {a.alert_type} [/]"
            side_style = "bold green" if a.aggressor_side == "BUY" else "bold red"
            lat_style = "green" if a.latency_us <= 50.0 else "bold red"

            table.add_row(
                f"#{a.alert_id}",
                type_badge,
                f"[{side_style}]{a.aggressor_side}[/]",
                f"${a.price:,.2f}",
                f"{a.quantity:,}",
                f"${a.notional_usd:,.2f}",
                f"[{lat_style}]{a.latency_us:.2f}μs[/]",
                f"[white]{a.details}[/]",
            )

        if not alerts:
            table.add_row(
                "",
                "[bold bright_green]NOMINAL[/]",
                "",
                "",
                "",
                "",
                "",
                "[dim]Zero SLA breaches or abnormal whale sweeps detected │ Lock-free SPSC IPC Active │ WAL Off Critical Path[/]",
            )

        return Panel(
            table,
            border_style="yellow",
            box=box.ROUNDED,
            title="[bold yellow]⚡ Institutional Whale Sweeps & SLA Anomaly Log[/]",
            title_align="left",
        )

    def render_string(
        self,
        l2_snapshot: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        latency_stats: Optional[LatencyStats] = None,
        throughput_info: Optional[Dict[str, Any]] = None,
        recent_trades: Optional[List[Dict[str, Any]]] = None,
        whale_alerts: Optional[List[WhaleAlert]] = None,
        width: int = 120,
    ) -> str:
        """Render dashboard layout into a string for testing, benchmarking, and snapshots."""
        buf = io.StringIO()
        test_console = Console(file=buf, width=width, force_terminal=True, color_system="standard")
        layout = self.build_layout(
            l2_snapshot=l2_snapshot,
            latency_stats=latency_stats,
            throughput_info=throughput_info,
            recent_trades=recent_trades,
            whale_alerts=whale_alerts,
        )
        test_console.print(layout)
        return buf.getvalue()


def _get_nonblocking_key() -> Optional[str]:
    """Poll non-blocking single character keyboard input across platforms."""
    try:
        if sys.platform == "win32":
            import msvcrt
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch in (b"\x00", b"\xe0"):
                    msvcrt.getch()
                    return None
                return ch.decode("utf-8", errors="ignore").lower()
        else:
            import select
            r, _, _ = select.select([sys.stdin], [], [], 0)
            if r:
                return sys.stdin.read(1).lower()
    except Exception:
        pass
    return None


def run_interactive_simulation():
    """
    Standalone interactive TUI simulator with live hotkey controls for defense demonstrations.
    Launch with: `python -m chronos_match.monitor.dashboard` or `python main.py --demo`
    """
    console = Console()
    dashboard = TerminalDashboard(console=console)

    mid_price = 150.00
    order_id = 1
    total_orders = 0
    total_cancels = 0
    total_trades = 0
    total_shares = 0

    recent_trades: List[Dict[str, Any]] = []
    whale_alerts: List[WhaleAlert] = []
    alert_counter = 0

    is_paused = False
    rate_levels = [25_000, 50_000, 100_000, 250_000]
    rate_idx = 2
    hotkey_status: Optional[str] = "Press [B]=Whale Buy, [S]=Whale Sell, [C]=Cancel Storm, [Space]=Pause"
    hotkey_status_expiry = time.perf_counter() + 5.0

    print("[*] Launching ChronosMatch Interactive Terminal TUI Simulation...")
    time.sleep(0.5)

    try:
        with Live(
            dashboard.build_layout(),
            console=console,
            refresh_per_second=20,
            screen=True,
            auto_refresh=True,
        ) as live:
            while True:
                # 1. Process non-blocking keyboard input
                key = _get_nonblocking_key()
                force_whale_side: Optional[Side] = None

                if key == "q":
                    break
                elif key == " ":
                    is_paused = not is_paused
                    hotkey_status = "[PAUSED - PRESS SPACE TO RESUME]" if is_paused else "RESUMED LIVE STREAMING"
                    hotkey_status_expiry = time.perf_counter() + 999.0 if is_paused else time.perf_counter() + 2.0
                elif key == "b":
                    force_whale_side = Side.BUY
                    hotkey_status = "⚡ INJECTED AGGRESSIVE WHALE BUY SWEEP (10,000 SHARES)"
                    hotkey_status_expiry = time.perf_counter() + 3.0
                elif key == "s":
                    force_whale_side = Side.SELL
                    hotkey_status = "⚡ INJECTED AGGRESSIVE WHALE SELL SWEEP (10,000 SHARES)"
                    hotkey_status_expiry = time.perf_counter() + 3.0
                elif key == "c":
                    total_cancels += 1000
                    total_orders += 1000
                    hotkey_status = "⚡ CANCELLATION STORM: 1,000 ORDERS UNLINKED IN O(1) TIME"
                    hotkey_status_expiry = time.perf_counter() + 3.0
                elif key == "r":
                    rate_idx = (rate_idx + 1) % len(rate_levels)
                    hotkey_status = f"⚡ FLOW INGESTION RATE CYCLED TO: {rate_levels[rate_idx]:,} ORD/S"
                    hotkey_status_expiry = time.perf_counter() + 3.0
                elif key == "h":
                    hotkey_status = "HELP: [B]=Whale Buy | [S]=Whale Sell | [C]=Cancel | [R]=Rate | [Space]=Pause | [Q]=Quit"
                    hotkey_status_expiry = time.perf_counter() + 4.0

                now = time.perf_counter()
                active_banner = hotkey_status if (now < hotkey_status_expiry or is_paused) else None

                if not is_paused or force_whale_side is not None:
                    # 2. Random walk price
                    if force_whale_side == Side.BUY:
                        mid_price += 0.15
                    elif force_whale_side == Side.SELL:
                        mid_price -= 0.15
                    else:
                        mid_price += random.choice([-0.05, -0.01, 0.0, 0.01, 0.05])
                    mid_price = max(10.0, mid_price)

                    # 3. Generate simulated L2 book
                    bids = []
                    asks = []
                    for i in range(10):
                        bp = mid_price - 0.05 - (i * 0.05)
                        ap = mid_price + 0.05 + (i * 0.05)
                        bids.append({
                            "price": to_fixed_price(bp),
                            "volume": random.randint(500, 5000),
                            "order_count": random.randint(1, 8),
                        })
                        asks.append({
                            "price": to_fixed_price(ap),
                            "volume": random.randint(500, 5000),
                            "order_count": random.randint(1, 8),
                        })

                    # 4. Simulate order bursts & trades
                    target_rate = rate_levels[rate_idx]
                    batch_orders = int(target_rate / 20)  # Orders per frame (20 FPS)
                    batch_cancels = int(batch_orders * 0.15)
                    total_orders += batch_orders
                    total_cancels += batch_cancels

                    # Trade execution
                    trade_occurred = (force_whale_side is not None) or (random.random() < 0.85)
                    if trade_occurred:
                        side = force_whale_side if force_whale_side is not None else random.choice([Side.BUY, Side.SELL])
                        is_whale = (force_whale_side is not None) or (random.random() < 0.08)
                        qty = 10_000 if is_whale else random.choice([100, 200, 500, 1000])
                        exec_price = (mid_price + 0.05) if side == Side.BUY else (mid_price - 0.05)
                        lat_us = random.uniform(1.2, 4.8) if not is_whale else random.uniform(4.5, 18.0)
                        total_trades += 1
                        total_shares += qty
                        order_id += 1

                        tp_entry = {
                            "trade_id": total_trades,
                            "aggressor_side": int(side),
                            "price": to_fixed_price(exec_price),
                            "quantity": qty,
                            "latency_ns": int(lat_us * 1000),
                        }
                        if len(recent_trades) >= 12:
                            recent_trades.pop(0)
                        recent_trades.append(tp_entry)

                        if is_whale:
                            alert_counter += 1
                            whale_alerts.append(
                                WhaleAlert(
                                    alert_id=alert_counter,
                                    timestamp_ns=time.perf_counter_ns(),
                                    trade_id=total_trades,
                                    buy_order_id=order_id + 10,
                                    sell_order_id=order_id + 20,
                                    symbol_id=1,
                                    aggressor_side="BUY" if side == Side.BUY else "SELL",
                                    price=exec_price,
                                    quantity=qty,
                                    notional_usd=qty * exec_price,
                                    latency_us=lat_us,
                                    alert_type="WHALE_SWEEP" if force_whale_side is not None else random.choice(["WHALE_SWEEP", "LARGE_BLOCK"]),
                                    details=f"Block Execution: {qty:,} shares @ ${exec_price:,.2f}",
                                )
                            )
                            if len(whale_alerts) > 5:
                                whale_alerts.pop(0)

                    # Simulated stats
                    lat_stats = LatencyStats(
                        sample_count=total_orders,
                        min_us=1.1,
                        max_us=38.4,
                        mean_us=3.8,
                        p50_us=3.2 + random.uniform(-0.2, 0.2),
                        p90_us=7.2 + random.uniform(-0.4, 0.4),
                        p99_us=16.5 + random.uniform(-0.8, 0.8),
                        p999_us=28.9,
                    )

                    tp_info = {
                        "orders_processed": total_orders,
                        "cancellations_processed": total_cancels,
                        "trades_generated": total_trades,
                        "shares_traded": total_shares,
                        "best_bid": bids[0]["price"],
                        "best_ask": asks[0]["price"],
                        "spread": asks[0]["price"] - bids[0]["price"],
                        "orders_per_sec": float(target_rate) + random.uniform(-5000, 5000),
                    }

                    layout = dashboard.build_layout(
                        l2_snapshot={"bids": bids, "asks": asks},
                        latency_stats=lat_stats,
                        throughput_info=tp_info,
                        recent_trades=recent_trades,
                        whale_alerts=whale_alerts,
                        hotkey_banner=active_banner,
                    )
                    live.update(layout)

                elif is_paused:
                    # Update layout with paused banner without advancing market simulation
                    layout = dashboard.build_layout(
                        l2_snapshot={"bids": bids, "asks": asks},
                        latency_stats=lat_stats,
                        throughput_info=tp_info,
                        recent_trades=recent_trades,
                        whale_alerts=whale_alerts,
                        hotkey_banner=active_banner,
                    )
                    live.update(layout)

                time.sleep(0.05)

    except KeyboardInterrupt:
        console.print("\n[bold green]✓ Simulation terminated cleanly.[/bold green]")


if __name__ == "__main__":
    run_interactive_simulation()
