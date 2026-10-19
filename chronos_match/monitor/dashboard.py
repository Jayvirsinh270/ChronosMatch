"""
ChronosMatch: Real-Time High-Frequency Trading Terminal Dashboard.
Cross-platform Terminal UI built with Rich. Displays:
1. System Status & Header Banner (Zero-Copy IPC, Cython nogil, WAL Ledger).
2. KPI Cards: Throughput, Trade Count, Current Spread, Nanosecond Latency SLA.
3. Level 2 Depth Ladder: Top 10 Bids and Asks with visual volume depth bars.
4. Live Trade Execution Ticker with microsecond latency metrics.
5. Real-time Whale Sweep & SLA Breach Alert Log.
"""

from datetime import datetime
import io
import time
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chronos_match.protocol import from_fixed_price, Side
from chronos_match.monitor.metrics import LatencyStats
from chronos_match.monitor.whale_detector import WhaleAlert


class TerminalDashboard:
    """
    Real-Time Terminal User Interface (TUI) Dashboard for ChronosMatch.
    """

    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console()

    def build_layout(
        self,
        l2_snapshot: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        latency_stats: Optional[LatencyStats] = None,
        throughput_info: Optional[Dict[str, Any]] = None,
        recent_trades: Optional[List[Dict[str, Any]]] = None,
        whale_alerts: Optional[List[WhaleAlert]] = None,
    ) -> Layout:
        """
        Construct a responsive Rich Layout hierarchy from current engine snapshots.
        """
        layout = Layout()

        # Split into Header, KPI Cards, Main Body (L2 Book + Ticker), and Bottom Alert Log
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="kpis", size=6),
            Layout(name="main_body", ratio=2),
            Layout(name="alerts", size=8),
        )

        layout["main_body"].split_row(
            Layout(name="order_book", ratio=1),
            Layout(name="trade_ticker", ratio=1),
        )

        # 1. Header Panel
        layout["header"].update(self._build_header())

        # 2. KPI Cards Panel
        layout["kpis"].update(self._build_kpi_cards(throughput_info, latency_stats))

        # 3. L2 Order Book Depth Table
        layout["order_book"].update(self._build_order_book_panel(l2_snapshot))

        # 4. Live Trade Execution Ticker Table
        layout["trade_ticker"].update(self._build_trade_ticker_panel(recent_trades))

        # 5. Whale & SLA Breach Alerts Panel
        layout["alerts"].update(self._build_alerts_panel(whale_alerts))

        return layout

    def _build_header(self) -> Panel:
        """Generate high-contrast header banner."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        title_text = Text()
        title_text.append("⚡ CHRONOS MATCH ", style="bold cyan")
        title_text.append("│ ZERO-COPY HFT MATCHING ENGINE │ ", style="bold white")
        title_text.append("PURE-C NOGIL • SHM SPSC IPC • SQLITE WAL ", style="dim green")
        title_text.append(f"│ {now_str} ", style="yellow")
        title_text.append("● NOMINAL", style="bold green")

        return Panel(
            title_text,
            style="bold white on blue",
            border_style="bright_blue",
        )

    def _build_kpi_cards(
        self,
        throughput_info: Optional[Dict[str, Any]],
        latency_stats: Optional[LatencyStats],
    ) -> Table:
        """Generate top-row metric cards table."""
        info = throughput_info or {}
        orders_proc = info.get("orders_processed", 0)
        cancels_proc = info.get("cancellations_processed", 0)
        trades_gen = info.get("trades_generated", 0)
        shares_traded = info.get("shares_traded", 0)
        rate = info.get("orders_per_sec", 0.0)

        best_bid = from_fixed_price(info.get("best_bid", 0))
        best_ask = from_fixed_price(info.get("best_ask", 0))
        spread = from_fixed_price(info.get("spread", 0))

        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)

        # Card 1: Throughput
        c1 = Text()
        c1.append("Throughput & Ingestion\n", style="bold cyan")
        c1.append(f"Orders: {orders_proc:,} ", style="white")
        c1.append(f"({cancels_proc:,} can)\n", style="dim")
        c1.append(f"Rate: {rate:,.0f} ord/s", style="bold green" if rate > 0 else "dim")

        # Card 2: Market Spread
        c2 = Text()
        c2.append("Market Top-of-Book\n", style="bold cyan")
        if best_bid > 0 and best_ask > 0:
            c2.append(f"Bid: ${best_bid:,.2f}  ", style="bold green")
            c2.append(f"Ask: ${best_ask:,.2f}\n", style="bold red")
            c2.append(f"Spread: ${spread:,.2f} ({(spread/best_bid)*10000:.1f} bps)", style="bold yellow")
        else:
            c2.append("Book Empty\nWaiting for Orders", style="dim")

        # Card 3: Execution Latency SLA
        c3 = Text()
        c3.append("Wire-to-Match Latency\n", style="bold cyan")
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

        # Card 4: Matched Volume
        c4 = Text()
        c4.append("Matched Liquidity\n", style="bold cyan")
        c4.append(f"Trades: {trades_gen:,}\n", style="bold white")
        c4.append(f"Volume: {shares_traded:,} shares", style="bold yellow")

        grid.add_row(
            Panel(c1, border_style="cyan"),
            Panel(c2, border_style="cyan"),
            Panel(c3, border_style="cyan"),
            Panel(c4, border_style="cyan"),
        )
        return grid

    def _build_order_book_panel(
        self, l2_snapshot: Optional[Dict[str, List[Dict[str, Any]]]]
    ) -> Panel:
        """Render Level 2 Order Book Depth Ladder with visual volume bars."""
        table = Table(
            title="LEVEL 2 MARKET DEPTH LADDER",
            title_style="bold magenta",
            expand=True,
            show_header=True,
            header_style="bold white on navy_blue",
            box=None,
        )
        table.add_column("Orders", justify="right", style="dim", width=7)
        table.add_column("Bid Depth", justify="left", width=12)
        table.add_column("Bid Qty", justify="right", style="bold green", width=10)
        table.add_column("Price", justify="center", style="bold white", width=12)
        table.add_column("Ask Qty", justify="left", style="bold red", width=10)
        table.add_column("Ask Depth", justify="left", width=12)
        table.add_column("Orders", justify="left", style="dim", width=7)

        snapshot = l2_snapshot or {"bids": [], "asks": []}
        bids = snapshot.get("bids", [])
        asks = snapshot.get("asks", [])

        # Find max volume for proportional bar rendering
        max_vol = 1
        for b in bids:
            if b["volume"] > max_vol:
                max_vol = b["volume"]
        for a in asks:
            if a["volume"] > max_vol:
                max_vol = a["volume"]

        # Render Top Asks (in reverse order so lowest ask is near the spread)
        asks_to_show = list(reversed(asks[:10]))
        for ask in asks_to_show:
            p_float = from_fixed_price(ask["price"])
            vol = ask["volume"]
            cnt = ask.get("order_count", 1)
            bar_len = int((vol / max_vol) * 10) if max_vol > 0 else 0
            bar = "█" * max(1, bar_len)
            table.add_row(
                "",
                "",
                "",
                f"${p_float:,.2f}",
                f"{vol:,}",
                f"[red]{bar}[/]",
                str(cnt),
            )

        # Mid-market Spread Divider
        if bids and asks:
            best_bid_p = from_fixed_price(bids[0]["price"])
            best_ask_p = from_fixed_price(asks[0]["price"])
            spread_p = best_ask_p - best_bid_p
            spread_str = f"── SPREAD: ${spread_p:,.2f} ──"
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
            bar_len = int((vol / max_vol) * 10) if max_vol > 0 else 0
            bar = "█" * max(1, bar_len)
            table.add_row(
                str(cnt),
                f"[green]{bar:>10}[/]",
                f"{vol:,}",
                f"${p_float:,.2f}",
                "",
                "",
                "",
            )

        return Panel(table, border_style="blue", title="Order Book")

    def _build_trade_ticker_panel(
        self, recent_trades: Optional[List[Dict[str, Any]]]
    ) -> Panel:
        """Render recent executed trade executions stream."""
        table = Table(
            title="LIVE TRADE EXECUTION STREAM",
            title_style="bold magenta",
            expand=True,
            show_header=True,
            header_style="bold white on navy_blue",
            box=None,
        )
        table.add_column("Trade ID", justify="right", style="dim", width=9)
        table.add_column("Side", justify="center", width=6)
        table.add_column("Price", justify="right", width=10)
        table.add_column("Quantity", justify="right", width=9)
        table.add_column("Notional", justify="right", width=12)
        table.add_column("Latency", justify="right", width=10)

        trades = recent_trades or []
        for t in trades[:12]:
            trade_id = t.get("trade_id", 0)
            side_val = t.get("aggressor_side", 0)
            side_str = "BUY" if side_val == int(Side.BUY) else "SELL"
            side_style = "bold green" if side_str == "BUY" else "bold red"

            # Check if price is fixed-point or float
            raw_price = t.get("price", t.get("execution_price", 0))
            price_float = from_fixed_price(raw_price) if raw_price > 100_000 else float(raw_price)
            qty = t.get("quantity", 0)
            notional = price_float * qty

            latency_ns = t.get("latency_ns", 0)
            latency_us = latency_ns / 1000.0
            lat_style = "green" if latency_us <= 20.0 else ("yellow" if latency_us <= 50.0 else "bold red")

            table.add_row(
                f"#{trade_id}",
                f"[{side_style}]{side_str}[/]",
                f"${price_float:,.2f}",
                f"{qty:,}",
                f"${notional:,.2f}",
                f"[{lat_style}]{latency_us:.2f}μs[/]",
            )

        if not trades:
            table.add_row("", "", "[dim]No executions yet[/]", "", "", "")

        return Panel(table, border_style="blue", title="Executions")

    def _build_alerts_panel(
        self, whale_alerts: Optional[List[WhaleAlert]]
    ) -> Panel:
        """Render real-time institutional flow and SLA breach alerts."""
        table = Table(
            expand=True,
            show_header=True,
            header_style="bold yellow on navy_blue",
            box=None,
        )
        table.add_column("Alert #", justify="right", width=7)
        table.add_column("Type", justify="center", width=14)
        table.add_column("Side", justify="center", width=6)
        table.add_column("Price", justify="right", width=10)
        table.add_column("Shares", justify="right", width=10)
        table.add_column("Notional USD", justify="right", width=14)
        table.add_column("Latency", justify="right", width=10)
        table.add_column("Event Details", justify="left")

        alerts = whale_alerts or []
        for a in alerts[-4:]:  # Show latest 4 alerts
            type_style = (
                "bold yellow"
                if a.alert_type == "WHALE_SWEEP"
                else ("bold cyan" if a.alert_type == "LARGE_BLOCK" else "bold red")
            )
            side_style = "bold green" if a.aggressor_side == "BUY" else "bold red"
            lat_style = "green" if a.latency_us <= 50.0 else "bold red"

            table.add_row(
                f"#{a.alert_id}",
                f"[{type_style}]{a.alert_type}[/]",
                f"[{side_style}]{a.aggressor_side}[/]",
                f"${a.price:,.2f}",
                f"{a.quantity:,}",
                f"${a.notional_usd:,.2f}",
                f"[{lat_style}]{a.latency_us:.2f}μs[/]",
                f"[white]{a.details}[/]",
            )

        if not alerts:
            table.add_row("", "[dim]NOMINAL[/]", "", "", "", "", "", "[dim]Zero SLA breaches or abnormal whale sweeps detected[/]")

        return Panel(
            table,
            border_style="yellow",
            title="⚡ Institutional Whale Sweeps & SLA Anomaly Log",
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
        """Render dashboard layout into a string for testing and snapshots."""
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
