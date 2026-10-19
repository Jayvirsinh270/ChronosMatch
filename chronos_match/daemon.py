"""
ChronosMatch: Multi-Process Daemon Architecture.
Orchestrates 4 independent OS processes communicating exclusively over lock-free
shared memory IPC buses and non-blocking telemetry channels:
  1. Ingestion Process: AsyncMarketFirehose generating 100k+ orders/sec.
  2. Matching Engine Process: Pure-C nogil Limit Order Book with nanosecond profiling.
  3. Audit Ledger Process: SQLite WAL worker persisting trade batches off the critical path.
  4. Terminal UI Dashboard Process: Rich 30-60 FPS live L2 depth and metrics dashboard.
"""

import asyncio
import multiprocessing as mp
import os
import signal
import sys
import time
from typing import Dict, List, Optional, Any

from chronos_match.protocol import (
    ORDER_SIZE,
    TRADE_SIZE,
    OrderSide,
    OrderType,
    to_fixed_price,
    from_fixed_price,
    TradePacket,
)
from chronos_match.ring_buffer import RingBuffer
from chronos_match.engine.order_book_cy import FastOrderBook
from chronos_match.market.distribution import MarketDistribution
from chronos_match.market.firehose import AsyncMarketFirehose
from chronos_match.monitor.metrics import LatencyProfiler, LatencyStats
from chronos_match.monitor.whale_detector import WhaleDetector, WhaleAlert
from chronos_match.monitor.dashboard import TerminalDashboard
from chronos_match.ledger.audit_logger import AuditLedger, AuditLedgerWorker


def run_firehose_worker(
    in_shm_path: str,
    capacity: int,
    total_orders: int,
    target_rate: int,
    stop_event: mp.Event,
) -> None:
    """
    Child Process 1: Market Firehose Ingestion Worker.
    Pumps Poisson-distributed order flow into the input ring buffer at high throughput.
    """
    try:
        ring = RingBuffer(
            filepath=in_shm_path,
            capacity=capacity,
            slot_size=ORDER_SIZE,
            create=False,
        )
        dist = MarketDistribution()
        firehose = AsyncMarketFirehose(distribution=dist)

        orders_to_send = total_orders if total_orders > 0 else 10_000_000
        firehose.sync_blast_to_ring_buffer(
            ring_buffer=ring,
            total_orders=orders_to_send,
        )
    except Exception as e:
        print(f"[Firehose Worker Error] {e}", file=sys.stderr)
    finally:
        try:
            ring.close()
        except Exception:
            pass


def run_engine_worker(
    in_shm_path: str,
    out_shm_path: str,
    capacity: int,
    stop_event: mp.Event,
    ui_queue: Optional[mp.Queue] = None,
    stats_dict: Optional[Any] = None,
    arena_capacity: int = 150_000,
) -> None:
    """
    Child Process 2: Pure-C nogil Matching Engine Core Worker.
    Consumes raw orders, executes Price-Time priority matching in Cython,
    tracks wire-to-match latency, forwards trades to outbound bus, and inspects whales.
    """
    try:
        in_buf = RingBuffer(
            filepath=in_shm_path,
            capacity=capacity,
            slot_size=ORDER_SIZE,
            create=False,
        )
        out_buf = RingBuffer(
            filepath=out_shm_path,
            capacity=capacity,
            slot_size=TRADE_SIZE,
            create=False,
        )

        book = FastOrderBook(arena_capacity=arena_capacity)
        profiler = LatencyProfiler(window_size=100_000)
        whale_detector = WhaleDetector()

        orders_processed = 0
        cancels_processed = 0
        trades_generated = 0
        shares_traded = 0

        last_ui_update = time.perf_counter()
        recent_trades: List[Dict[str, Any]] = []

        while True:
            order = in_buf.read_order()
            if order is None:
                if stop_event.is_set():
                    break
                time.sleep(0.0001)
                continue

            t_matched = time.perf_counter_ns()
            orders_processed += 1

            # 1. CANCELLATION
            if order.order_type == int(OrderType.CANCEL):
                if book.cancel_order(order.order_id):
                    cancels_processed += 1
                if order.timestamp_in_ns > 0:
                    profiler.record_delta(order.timestamp_in_ns, t_matched)

            # 2. LIMIT / MARKET ORDER MATCHING
            else:
                matches = book.add_limit_order(
                    order_id=order.order_id,
                    client_id=order.client_id,
                    price=order.price,
                    quantity=order.quantity,
                    side=order.side,
                    symbol_id=order.symbol_id,
                    timestamp_ns=order.timestamp_in_ns,
                )

                if order.timestamp_in_ns > 0:
                    profiler.record_delta(order.timestamp_in_ns, t_matched)

                if matches:
                    for m in matches:
                        trades_generated += 1
                        shares_traded += m["quantity"]
                        lat_ns = t_matched - order.timestamp_in_ns if order.timestamp_in_ns > 0 else 0

                        tp = TradePacket(
                            trade_id=m["trade_id"],
                            buy_order_id=m["buy_order_id"],
                            sell_order_id=m["sell_order_id"],
                            price=m["execution_price"],
                            quantity=m["quantity"],
                            symbol_id=order.symbol_id,
                            aggressor_side=m["aggressor_side"],
                            match_timestamp_ns=t_matched,
                            latency_ns=lat_ns,
                        )
                        out_buf.write_trade(tp)

                        # Whale anomaly inspection
                        whale_detector.inspect_trade(tp)

                        # Cache recent execution for UI
                        if len(recent_trades) >= 15:
                            recent_trades.pop(0)
                        recent_trades.append({
                            "trade_id": tp.trade_id,
                            "aggressor_side": tp.aggressor_side,
                            "price": tp.price,
                            "quantity": tp.quantity,
                            "latency_ns": tp.latency_ns,
                        })

            # Periodic non-blocking telemetry broadcast to UI (10-20 FPS)
            now = time.perf_counter()
            if ui_queue is not None and (now - last_ui_update) >= 0.05:  # Every 50ms
                stats = profiler.compute_stats()
                l2 = book.get_l2_snapshot(depth=10)
                tp_info = {
                    "orders_processed": orders_processed,
                    "cancellations_processed": cancels_processed,
                    "trades_generated": trades_generated,
                    "shares_traded": shares_traded,
                    "best_bid": book.best_bid_price,
                    "best_ask": book.best_ask_price,
                    "spread": book.spread,
                    "orders_per_sec": orders_processed / max(0.001, (now - last_ui_update)),
                }

                payload = {
                    "l2": l2,
                    "stats": stats,
                    "throughput": tp_info,
                    "recent_trades": list(recent_trades),
                    "whale_alerts": whale_detector.get_recent_alerts(limit=5),
                }

                try:
                    # Non-blocking put; if UI is saturated, drop the telemetry frame without pausing matching
                    ui_queue.put_nowait(payload)
                except Exception:
                    pass

                last_ui_update = now

        # Update final stats if dict provided
        if stats_dict is not None:
            final_stats = profiler.compute_stats()
            stats_dict["orders_processed"] = orders_processed
            stats_dict["cancellations_processed"] = cancels_processed
            stats_dict["trades_generated"] = trades_generated
            stats_dict["shares_traded"] = shares_traded
            if final_stats:
                stats_dict["p50_us"] = final_stats.p50_us
                stats_dict["p99_us"] = final_stats.p99_us
    except Exception as e:
        print(f"[Engine Worker Error] {e}", file=sys.stderr)
    finally:
        try:
            in_buf.close()
            out_buf.close()
        except Exception:
            pass


def run_ledger_worker(
    out_shm_path: str,
    capacity: int,
    db_path: str,
    stop_event: mp.Event,
    batch_size: int = 2000,
) -> None:
    """
    Child Process 3: Asynchronous SQLite WAL Persistence Worker.
    Drains outbound trades and commits atomic batches off the engine critical path.
    """
    try:
        out_buf = RingBuffer(
            filepath=out_shm_path,
            capacity=capacity,
            slot_size=TRADE_SIZE,
            create=False,
        )
        ledger = AuditLedger(db_path=db_path)
        worker = AuditLedgerWorker(
            trade_buffer=out_buf,
            ledger=ledger,
            batch_size=batch_size,
            flush_interval_ms=50.0,
        )

        while True:
            drained = worker.poll_and_buffer(max_read=5000)
            if drained == 0:
                if stop_event.is_set():
                    break
                time.sleep(0.001)

        # Final drain upon stopping
        worker.drain_all()
        ledger.close()
        out_buf.close()
    except Exception as e:
        print(f"[Ledger Worker Error] {e}", file=sys.stderr)


def run_dashboard_worker(
    ui_queue: mp.Queue,
    stop_event: mp.Event,
    refresh_hz: float = 15.0,
) -> None:
    """
    Child Process 4: Real-Time Rich Terminal Dashboard UI Worker.
    Renders live Level 2 depth ladder, spreads, trade executions, and latency metrics.
    """
    try:
        from rich.live import Live
        from rich.console import Console

        console = Console()
        dashboard = TerminalDashboard(console=console)

        latest_payload: Optional[Dict[str, Any]] = None
        sleep_interval = 1.0 / refresh_hz

        with Live(
            dashboard.build_layout(),
            console=console,
            refresh_per_second=int(refresh_hz),
            screen=False,
            auto_refresh=True,
        ) as live:
            while not stop_event.is_set():
                # Drain latest telemetry update from queue
                updated = False
                while not ui_queue.empty():
                    try:
                        latest_payload = ui_queue.get_nowait()
                        updated = True
                    except Exception:
                        break

                if latest_payload is not None and updated:
                    layout = dashboard.build_layout(
                        l2_snapshot=latest_payload.get("l2"),
                        latency_stats=latest_payload.get("stats"),
                        throughput_info=latest_payload.get("throughput"),
                        recent_trades=latest_payload.get("recent_trades"),
                        whale_alerts=latest_payload.get("whale_alerts"),
                    )
                    live.update(layout)

                time.sleep(sleep_interval)
    except Exception as e:
        pass


class ChronosDaemon:
    """
    Master Multi-Process Coordinator managing shared memory lifecycles,
    worker supervision, and graceful shutdown across all 4 engine processes.
    """

    def __init__(
        self,
        shm_dir: Optional[str] = None,
        capacity: int = 65536,
        total_orders: int = 100_000,
        target_rate: int = 100_000,
        db_path: str = "chronos_audit.db",
        enable_ui: bool = True,
    ):
        self.shm_dir = shm_dir or os.path.abspath(".chronos_shm")
        os.makedirs(self.shm_dir, exist_ok=True)

        self.capacity = capacity
        self.total_orders = total_orders
        self.target_rate = target_rate
        self.db_path = db_path
        self.enable_ui = enable_ui

        self.in_shm_path = os.path.join(self.shm_dir, "order_bus.ring")
        self.out_shm_path = os.path.join(self.shm_dir, "trade_bus.ring")

        self.stop_event = mp.Event()
        self.ui_queue = mp.Queue(maxsize=100) if enable_ui else None

        self.processes: List[mp.Process] = []
        self._in_ring_master: Optional[RingBuffer] = None
        self._out_ring_master: Optional[RingBuffer] = None

    def initialize_shared_memory(self) -> None:
        """Create and initialize the physical shared memory ring buffer files."""
        # Cleanup any old residual files
        for p in [self.in_shm_path, self.out_shm_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

        self._in_ring_master = RingBuffer(
            filepath=self.in_shm_path,
            capacity=self.capacity,
            slot_size=ORDER_SIZE,
            create=True,
        )
        self._out_ring_master = RingBuffer(
            filepath=self.out_shm_path,
            capacity=self.capacity,
            slot_size=TRADE_SIZE,
            create=True,
        )

    def start(self) -> None:
        """Spawn all 4 isolated OS worker processes."""
        self.initialize_shared_memory()
        self.stop_event.clear()

        # 1. Matching Engine Process (Core)
        p_engine = mp.Process(
            target=run_engine_worker,
            args=(
                self.in_shm_path,
                self.out_shm_path,
                self.capacity,
                self.stop_event,
                self.ui_queue,
            ),
            name="ChronosEngineCore",
        )
        self.processes.append(p_engine)

        # 2. SQLite WAL Audit Ledger Process
        p_ledger = mp.Process(
            target=run_ledger_worker,
            args=(
                self.out_shm_path,
                self.capacity,
                self.db_path,
                self.stop_event,
            ),
            name="ChronosAuditLedger",
        )
        self.processes.append(p_ledger)

        # 3. Market Firehose Producer Process
        p_firehose = mp.Process(
            target=run_firehose_worker,
            args=(
                self.in_shm_path,
                self.capacity,
                self.total_orders,
                self.target_rate,
                self.stop_event,
            ),
            name="ChronosMarketFirehose",
        )
        self.processes.append(p_firehose)

        # 4. Rich Terminal Dashboard UI Process
        if self.enable_ui and self.ui_queue is not None:
            p_ui = mp.Process(
                target=run_dashboard_worker,
                args=(self.ui_queue, self.stop_event),
                name="ChronosTerminalUI",
            )
            self.processes.append(p_ui)

        # Start all child processes
        for p in self.processes:
            p.daemon = True
            p.start()

    def wait_for_completion(self, timeout_s: Optional[float] = None) -> None:
        """Wait for firehose completion or timeout."""
        p_firehose = next((p for p in self.processes if p.name == "ChronosMarketFirehose"), None)
        if p_firehose:
            p_firehose.join(timeout=timeout_s)

    def stop(self, timeout_s: float = 3.0) -> None:
        """Gracefully terminate all workers and unlink shared memory."""
        self.stop_event.set()

        for p in self.processes:
            p.join(timeout=timeout_s)
            if p.is_alive():
                p.terminate()

        self.processes.clear()

        # Close and unlink shared memory
        if self._in_ring_master:
            self._in_ring_master.close()
            self._in_ring_master = None
        if self._out_ring_master:
            self._out_ring_master.close()
            self._out_ring_master = None

        for p in [self.in_shm_path, self.out_shm_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
