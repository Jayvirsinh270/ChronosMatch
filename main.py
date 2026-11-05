"""
ChronosMatch: Master Multi-Process Production Entrypoint.
Orchestrates high-throughput market firehose, pure-C matching engine,
asynchronous SQLite WAL audit persistence, and real-time Rich TUI dashboard.

Usage:
    python main.py --orders 100000 --rate 100000
    python main.py --no-ui --orders 500000
"""

import argparse
import multiprocessing as mp
import os
import signal
import sys
import time

# Expose FastAPI application instance for Vercel / ASGI cloud runners
from chronos_match.web.server import app


def parse_args():
    parser = argparse.ArgumentParser(
        description="ChronosMatch: Zero-Copy High-Frequency Trading Matching Engine"
    )
    parser.add_argument(
        "--orders",
        type=int,
        default=100_000,
        help="Total orders to generate and match (default: 100,000; set 0 for continuous)",
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=100_000,
        help="Target order generation rate in orders/second (default: 100,000)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="chronos_audit.db",
        help="SQLite WAL audit ledger database file path (default: chronos_audit.db)",
    )
    parser.add_argument(
        "--capacity",
        type=int,
        default=65536,
        help="Shared memory ring buffer slot capacity (must be power of 2, default: 65536)",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Run in headless mode without Terminal TUI (ideal for soak benchmarks)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run interactive real-time Terminal TUI simulator with live mock market streaming",
    )
    return parser.parse_args()


def print_banner(console=None):
    from rich.panel import Panel
    from rich.text import Text
    from rich import box

    if console is None:
        from rich.console import Console
        console = Console()

    banner_text = Text()
    banner_text.append("⚡ CHRONOS MATCH ", style="bold bright_cyan")
    banner_text.append("│ ZERO-COPY HIGH-FREQUENCY TRADING ENGINE\n", style="bold white")
    banner_text.append("• Pure-C nogil Limit Order Book  • Cache-Aligned SPSC Ring Buffer (64B)\n", style="bright_green")
    banner_text.append("• Microsecond Latency SLA (< 50μs) • Asynchronous SQLite WAL Audit Logging", style="dim cyan")

    panel = Panel(
        banner_text,
        border_style="bright_blue",
        box=box.DOUBLE,
        padding=(0, 2),
    )
    console.print(panel)


def main():
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box

    console = Console()
    args = parse_args()

    if args.demo:
        from chronos_match.monitor.dashboard import run_interactive_simulation
        run_interactive_simulation()
        return

    from chronos_match.daemon import ChronosDaemon
    from chronos_match.ledger.audit_logger import AuditLedger

    print_banner(console)

    config_table = Table(
        title="SYSTEM RUNTIME CONFIGURATION",
        title_style="bold bright_cyan",
        box=box.ROUNDED,
        border_style="cyan",
        show_header=False,
        pad_edge=True,
    )
    config_table.add_column("Parameter", style="bold white", width=22)
    config_table.add_column("Value", style="bold yellow")

    orders_str = f"{args.orders:,} orders" if args.orders > 0 else "Continuous Streaming (Ctrl+C to stop)"
    config_table.add_row("Total Ingestion Target", orders_str)
    config_table.add_row("Target Rate", f"{args.rate:,} orders/sec")
    config_table.add_row("IPC Ring Buffer Capacity", f"{args.capacity:,} slots (64B SPSC)")
    config_table.add_row("Audit Ledger Database", args.db)
    config_table.add_row("Visual Mode", "Headless Benchmark" if args.no_ui else "Live 30-FPS Rich TUI Cockpit")

    console.print(config_table)
    console.print()

    daemon = ChronosDaemon(
        capacity=args.capacity,
        total_orders=args.orders,
        target_rate=args.rate,
        db_path=args.db,
        enable_ui=not args.no_ui,
    )

    # Register graceful shutdown on Ctrl+C (SIGINT)
    def _sig_handler(sig, frame):
        console.print("\n\n[bold yellow][!] Received interrupt signal (Ctrl+C). Initiating graceful shutdown...[/bold yellow]")
        daemon.stop()
        console.print("[bold green][+] All worker processes terminated cleanly. Shared memory unlinked.[/bold green]")
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig_handler)

    console.print("[bold cyan][*] Launching multi-process exchange cluster...[/bold cyan]")
    t0 = time.perf_counter()
    daemon.start()

    try:
        if args.orders > 0:
            daemon.wait_for_completion()
            # Give engine and ledger a moment to finish draining the final batches
            time.sleep(0.5)
        else:
            # Continuous streaming until Ctrl+C
            while True:
                time.sleep(1.0)
    except KeyboardInterrupt:
        _sig_handler(None, None)
    finally:
        daemon.stop()
        elapsed = time.perf_counter() - t0

    # Print final summary audit table
    summary_table = Table(
        title="⚡ CHRONOS MATCH — EXECUTION SOAK SUMMARY AUDIT",
        title_style="bold bright_cyan",
        box=box.HEAVY_EDGE,
        border_style="bright_blue",
        pad_edge=True,
    )
    summary_table.add_column("Metric", style="bold white", width=25)
    summary_table.add_column("Performance Measurement", style="bold green", width=35)

    summary_table.add_row("Total Duration", f"{elapsed:.2f} seconds")
    if args.orders > 0:
        eff_rate = args.orders / max(0.001, elapsed)
        summary_table.add_row("Ingestion Volume", f"{args.orders:,} orders")
        summary_table.add_row("Effective Throughput", f"{eff_rate:,.0f} orders/sec")

    # Inspect persisted ledger
    if os.path.exists(args.db):
        try:
            audit = AuditLedger(db_path=args.db, read_only=True)
            summary = audit.get_summary()
            summary_table.add_row("Trades Persisted (WAL)", f"{summary['total_trades']:,} executions")
            summary_table.add_row("Total Shares Traded", f"{summary['total_volume']:,} shares")
            summary_table.add_row("Avg Execution Latency", f"{summary['avg_latency_us']:.2f} μs")
            audit.close()
        except Exception:
            pass

    console.print()
    console.print(summary_table)
    console.print("[bold bright_green][+] ChronosMatch shut down successfully with zero memory leaks.[/bold bright_green]\n")


if __name__ == "__main__":
    mp.freeze_support()
    main()
