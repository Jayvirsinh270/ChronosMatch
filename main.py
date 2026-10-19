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

from chronos_match.daemon import ChronosDaemon
from chronos_match.ledger.audit_logger import AuditLedger


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
    return parser.parse_args()


def print_banner():
    banner = r"""
  ╔═══════════════════════════════════════════════════════════════════╗
  ║   CHRONOS MATCH // ZERO-COPY HIGH-FREQUENCY TRADING ENGINE        ║
  ║   Pure-C nogil LOB • Lock-Free SPSC IPC • Asynchronous SQLite WAL ║
  ╚═══════════════════════════════════════════════════════════════════╝
    """
    print(banner)


def main():
    args = parse_args()
    print_banner()

    print(f"[*] Configuration:")
    print(f"    • Total Orders:      {args.orders:,}" if args.orders > 0 else "    • Total Orders:      Continuous Streaming")
    print(f"    • Target Ingestion:  {args.rate:,} orders/sec")
    print(f"    • IPC Ring Capacity: {args.capacity:,} slots")
    print(f"    • Audit Database:    {args.db}")
    print(f"    • UI Mode:           {'Headless' if args.no_ui else 'Live Rich TUI'}")
    print()

    daemon = ChronosDaemon(
        capacity=args.capacity,
        total_orders=args.orders,
        target_rate=args.rate,
        db_path=args.db,
        enable_ui=not args.no_ui,
    )

    # Register graceful shutdown on Ctrl+C (SIGINT)
    def _sig_handler(sig, frame):
        print("\n\n[!] Received interrupt signal (Ctrl+C). Initiating graceful shutdown...")
        daemon.stop()
        print("[+] All worker processes terminated cleanly. Shared memory unlinked.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig_handler)

    print("[*] Launching multi-process cluster...")
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

    # Print final summary audit
    print("\n" + "=" * 65)
    print("                EXECUTION SOAK SUMMARY AUDIT")
    print("=" * 65)
    print(f"  Total Duration:     {elapsed:.2f} seconds")
    if args.orders > 0:
        eff_rate = args.orders / max(0.001, elapsed)
        print(f"  Ingestion Volume:   {args.orders:,} orders")
        print(f"  Effective Rate:     {eff_rate:,.0f} orders/sec")

    # Inspect persisted ledger
    if os.path.exists(args.db):
        try:
            audit = AuditLedger(db_path=args.db, read_only=True)
            summary = audit.get_summary()
            print(f"  Trades Persisted:   {summary['total_trades']:,} executions in WAL")
            print(f"  Total Volume:       {summary['total_volume']:,} shares")
            print(f"  Avg Latency:        {summary['avg_latency_us']:.2f} μs")
            audit.close()
        except Exception:
            pass

    print("=" * 65)
    print("[+] ChronosMatch shut down successfully.\n")


if __name__ == "__main__":
    mp.freeze_support()
    main()
