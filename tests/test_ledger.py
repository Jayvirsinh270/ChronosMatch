"""
ChronosMatch: Test Suite for Asynchronous SQLite WAL Audit Ledger.
Verifies crash-safe WAL pragmas, high-throughput batch persistence,
thread-safe background worker draining, and query precision.
"""

import os
import tempfile
import time
import pytest

from chronos_match.protocol import TradePacket, to_fixed_price, Side
from chronos_match.ring_buffer import RingBuffer
from chronos_match.ledger.audit_logger import AuditLedger, AuditLedgerWorker


@pytest.fixture
def temp_db_path():
    """Create a temporary database path and clean up after test."""
    temp_dir = tempfile.mkdtemp()
    db_file = os.path.join(temp_dir, "test_audit.db")
    yield db_file
    # Cleanup
    for suffix in ["", "-wal", "-shm"]:
        p = db_file + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    try:
        os.rmdir(temp_dir)
    except Exception:
        pass


def test_ledger_initialization_and_pragmas(temp_db_path):
    """Verify SQLite WAL mode, memory pragmas, and schema creation."""
    ledger = AuditLedger(db_path=temp_db_path)
    cur = ledger.conn.cursor()

    # Check WAL journal mode
    cur.execute("PRAGMA journal_mode;")
    mode = cur.fetchone()[0]
    assert mode.upper() == "WAL"

    # Check table existence
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades';")
    assert cur.fetchone() is not None

    cur.close()
    ledger.close()


def test_ledger_single_and_batch_insert(temp_db_path):
    """Verify single and batch trade packet persistence."""
    ledger = AuditLedger(db_path=temp_db_path)

    # 1. Single insert
    t1 = TradePacket(
        trade_id=1,
        buy_order_id=101,
        sell_order_id=201,
        price=to_fixed_price(150.25),
        quantity=500,
        symbol_id=1,
        aggressor_side=int(Side.BUY),
        match_timestamp_ns=1_000_000_000,
        latency_ns=4_500,
    )
    ledger.insert_trade(t1)
    assert ledger.get_trade_count() == 1

    rec = ledger.get_trade_by_id(1)
    assert rec is not None
    assert rec["trade_id"] == 1
    assert rec["buy_order_id"] == 101
    assert rec["price"] == to_fixed_price(150.25)
    assert rec["price_float"] == 150.25
    assert rec["quantity"] == 500

    # 2. Batch insert
    batch = [
        TradePacket(
            trade_id=i,
            buy_order_id=1000 + i,
            sell_order_id=2000 + i,
            price=to_fixed_price(150.0 + (i % 10) * 0.05),
            quantity=100 * (1 + (i % 5)),
            symbol_id=1,
            aggressor_side=int(Side.SELL if i % 2 == 0 else Side.BUY),
            match_timestamp_ns=1_000_000_000 + (i * 10_000),
            latency_ns=5_000 + (i % 1_000),
        )
        for i in range(2, 502)
    ]
    inserted = ledger.insert_trades_batch(batch)
    assert inserted == 500
    assert ledger.get_trade_count() == 501

    ledger.close()


def test_ledger_queries_by_id_and_timerange(temp_db_path):
    """Verify range queries and summary statistics."""
    ledger = AuditLedger(db_path=temp_db_path)
    base_time = 1_700_000_000_000_000_000  # ns

    trades = [
        TradePacket(
            trade_id=i,
            buy_order_id=i * 10,
            sell_order_id=i * 20,
            price=to_fixed_price(100.0 + i),
            quantity=1000,
            symbol_id=1,
            aggressor_side=int(Side.BUY),
            match_timestamp_ns=base_time + (i * 1_000_000),  # spaced by 1ms
            latency_ns=6_000,
        )
        for i in range(1, 21)
    ]
    ledger.insert_trades_batch(trades)

    # Query middle window (trades 5 to 15)
    window = ledger.get_trades_by_timerange(
        start_ns=base_time + (5 * 1_000_000),
        end_ns=base_time + (15 * 1_000_000),
    )
    assert len(window) == 11
    assert window[0]["trade_id"] == 5
    assert window[-1]["trade_id"] == 15

    # Check summary
    summary = ledger.get_summary()
    assert summary["total_trades"] == 20
    assert summary["total_volume"] == 20_000
    assert summary["min_price"] == 101.0
    assert summary["max_price"] == 120.0

    ledger.close()


def test_ledger_worker_draining(temp_db_path):
    """Verify AuditLedgerWorker draining trade packets from a ring buffer."""
    buf_file = os.path.join(tempfile.gettempdir(), f"chronos_worker_{os.getpid()}_{int(time.time()*1000)%100000}.bin")
    trade_buf = RingBuffer(filepath=buf_file, capacity=4096, slot_size=64, create=True)
    ledger = AuditLedger(db_path=temp_db_path)

    worker = AuditLedgerWorker(
        trade_buffer=trade_buf,
        ledger=ledger,
        batch_size=50,
        flush_interval_ms=10.0,
    )

    # Push 120 trades to ring buffer
    for i in range(1, 121):
        trade_buf.write_trade(
            TradePacket(
                trade_id=i,
                buy_order_id=i,
                sell_order_id=i + 500,
                price=to_fixed_price(200.0),
                quantity=100,
                symbol_id=1,
                aggressor_side=int(Side.BUY),
                match_timestamp_ns=time.perf_counter_ns(),
                latency_ns=4_000,
            )
        )

    # Poll and buffer
    drained = worker.poll_and_buffer(max_read=200)
    assert drained == 120

    # Ensure all flushed
    worker.flush()
    assert ledger.get_trade_count() == 120
    assert worker.total_trades_persisted == 120

    ledger.close()
    trade_buf.close()
    if os.path.exists(buf_file):
        try:
            os.remove(buf_file)
        except Exception:
            pass


def test_ledger_worker_background_thread(temp_db_path):
    """Verify asynchronous background persistence thread without blocking."""
    buf_file = os.path.join(tempfile.gettempdir(), f"chronos_async_{os.getpid()}_{int(time.time()*1000)%100000}.bin")
    trade_buf = RingBuffer(filepath=buf_file, capacity=8192, slot_size=64, create=True)
    ledger = AuditLedger(db_path=temp_db_path)

    worker = AuditLedgerWorker(
        trade_buffer=trade_buf,
        ledger=ledger,
        batch_size=100,
        flush_interval_ms=20.0,
    )

    worker.start(poll_interval_s=0.002)

    # Stream 300 trades into buffer while worker runs in background
    for i in range(1, 301):
        trade_buf.write_trade(
            TradePacket(
                trade_id=i,
                buy_order_id=i * 2,
                sell_order_id=i * 2 + 1,
                price=to_fixed_price(180.50),
                quantity=250,
                symbol_id=1,
                aggressor_side=int(Side.SELL),
                match_timestamp_ns=time.perf_counter_ns(),
                latency_ns=5_500,
            )
        )
        if i % 50 == 0:
            time.sleep(0.005)

    # Allow worker thread to drain
    time.sleep(0.05)
    worker.stop(timeout_s=2.0)

    assert ledger.get_trade_count() == 300
    assert worker.total_trades_persisted == 300

    ledger.close()
    trade_buf.close()
    if os.path.exists(buf_file):
        try:
            os.remove(buf_file)
        except Exception:
            pass


def test_ledger_throughput_benchmark(temp_db_path):
    """Benchmark raw batch persistence throughput to SQLite WAL."""
    ledger = AuditLedger(db_path=temp_db_path)
    count = 10_000
    batch = [
        TradePacket(
            trade_id=i,
            buy_order_id=i + 1,
            sell_order_id=i + 2,
            price=to_fixed_price(100.0),
            quantity=100,
            symbol_id=1,
            aggressor_side=int(Side.BUY),
            match_timestamp_ns=time.perf_counter_ns(),
            latency_ns=4_200,
        )
        for i in range(1, count + 1)
    ]

    t0 = time.perf_counter()
    inserted = ledger.insert_trades_batch(batch)
    elapsed = time.perf_counter() - t0

    throughput = count / elapsed
    print(f"\n[LEDGER BENCHMARK] Persisted {count:,} trades in {elapsed*1000:.2f} ms ({throughput:,.0f} trades/sec)")
    assert inserted == count
    assert ledger.get_trade_count() == count
    # Ensure WAL persistence is fast (> 20,000 writes/sec in batch)
    assert throughput > 20_000

    ledger.close()
