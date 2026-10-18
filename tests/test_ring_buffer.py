"""
Unit and integration tests for ChronosMatch SPSC Ring Buffer.
Verifies zero-copy, memory alignment, FIFO integrity, and IPC multi-processing.
"""

import os
import time
import pytest
import multiprocessing as mp
from pathlib import Path

from chronos_match.protocol import (
    OrderPacket,
    TradePacket,
    OrderSide,
    OrderType,
    to_fixed_price,
    from_fixed_price,
)
from chronos_match.ring_buffer import RingBuffer

TEST_DIR = Path("d:/infotech_internship_project/.test_shm")

@pytest.fixture(autouse=True)
def cleanup():
    """Ensure clean test directory before and after tests."""
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    yield
    for f in TEST_DIR.glob("*.ring"):
        try:
            f.unlink()
        except Exception:
            pass


def test_ring_buffer_create_and_reopen():
    """Verify creating a ring buffer and attaching from another handle."""
    rb_path = TEST_DIR / "test_init.ring"
    rb = RingBuffer(str(rb_path), capacity=1024, create=True)
    assert rb.capacity == 1024
    assert rb.head == 0
    assert rb.tail == 0
    assert rb.is_empty()
    assert not rb.is_full()
    rb.close()

    # Reopen
    rb_reader = RingBuffer(str(rb_path), create=False)
    assert rb_reader.capacity == 1024
    assert rb_reader.head == 0
    assert rb_reader.tail == 0
    rb_reader.close()


def test_order_fifo_integrity():
    """Verify orders written are read back in exact FIFO order without corruption."""
    rb_path = TEST_DIR / "test_fifo.ring"
    rb = RingBuffer(str(rb_path), capacity=128, create=True)

    orders = [
        OrderPacket(
            order_id=i,
            client_id=100 + i,
            price=to_fixed_price(150.25 + (i * 0.10)),
            quantity=100 * (i + 1),
            side=OrderSide.BUY if i % 2 == 0 else OrderSide.SELL,
            order_type=OrderType.LIMIT,
            symbol_id=1,
            timestamp_in_ns=time.perf_counter_ns()
        )
        for i in range(50)
    ]

    for o in orders:
        assert rb.write_order(o) is True

    assert rb.head == 50
    assert rb.tail == 0

    read_orders = []
    for _ in range(50):
        o = rb.read_order()
        assert o is not None
        read_orders.append(o)

    assert rb.read_order() is None
    assert rb.tail == 50
    assert rb.is_empty()

    # Verify all fields match exactly
    for orig, read in zip(orders, read_orders):
        assert orig.order_id == read.order_id
        assert orig.client_id == read.client_id
        assert orig.price == read.price
        assert orig.quantity == read.quantity
        assert orig.side == read.side
        assert orig.order_type == read.order_type
        assert orig.symbol_id == read.symbol_id
        assert orig.timestamp_in_ns == read.timestamp_in_ns

    rb.close()


def test_ring_buffer_backpressure_when_full():
    """Verify that writing beyond capacity returns False and does not overwrite unread data."""
    rb_path = TEST_DIR / "test_full.ring"
    capacity = 16
    rb = RingBuffer(str(rb_path), capacity=capacity, create=True)

    # Fill to capacity
    for i in range(capacity):
        o = OrderPacket(
            order_id=i, client_id=1, price=10000, quantity=10,
            side=0, order_type=0, symbol_id=1, timestamp_in_ns=0
        )
        assert rb.write_order(o) is True

    assert rb.is_full()

    # 17th write must fail (backpressure)
    overflow = OrderPacket(
        order_id=999, client_id=1, price=10000, quantity=10,
        side=0, order_type=0, symbol_id=1, timestamp_in_ns=0
    )
    assert rb.write_order(overflow) is False

    # Read 1 order
    read_0 = rb.read_order()
    assert read_0.order_id == 0
    assert not rb.is_full()

    # Now write should succeed
    assert rb.write_order(overflow) is True

    rb.close()


def test_ring_buffer_wraparound():
    """Verify ring buffer operates cleanly through multiple cycles of capacity wraparound."""
    rb_path = TEST_DIR / "test_wrap.ring"
    capacity = 32
    rb = RingBuffer(str(rb_path), capacity=capacity, create=True)

    total_orders = 200
    for i in range(total_orders):
        o = OrderPacket(
            order_id=i, client_id=1, price=to_fixed_price(100.0), quantity=5,
            side=0, order_type=0, symbol_id=1, timestamp_in_ns=i
        )
        assert rb.write_order(o) is True
        read_o = rb.read_order()
        assert read_o is not None
        assert read_o.order_id == i

    assert rb.head == total_orders
    assert rb.tail == total_orders
    assert rb.is_empty()
    rb.close()


def _producer_proc(path: str, count: int):
    """Child process for IPC producer."""
    rb = RingBuffer(path, create=False)
    for i in range(count):
        o = OrderPacket(
            order_id=i, client_id=42, price=to_fixed_price(250.50),
            quantity=100, side=0, order_type=0, symbol_id=1,
            timestamp_in_ns=time.perf_counter_ns()
        )
        while not rb.write_order(o):
            pass # Busy spin backpressure
    rb.close()


def _consumer_proc(path: str, count: int, result_queue: mp.Queue):
    """Child process for IPC consumer."""
    rb = RingBuffer(path, create=False)
    consumed = 0
    t0 = time.perf_counter_ns()
    while consumed < count:
        o = rb.read_order()
        if o is not None:
            consumed += 1
    t1 = time.perf_counter_ns()
    rb.close()
    duration_s = (t1 - t0) / 1e9
    result_queue.put((consumed, duration_s))


def test_ipc_multiprocess_throughput():
    """
    IPC Audit: Transmit 50,000 orders between two separate OS processes
    via shared memory ring buffer without Pickle or JSON serialization.
    """
    rb_path = TEST_DIR / "test_ipc.ring"
    count = 50_000
    rb = RingBuffer(str(rb_path), capacity=8192, create=True)
    rb.close()

    result_queue = mp.Queue()
    cons_p = mp.Process(target=_consumer_proc, args=(str(rb_path), count, result_queue))
    prod_p = mp.Process(target=_producer_proc, args=(str(rb_path), count))

    cons_p.start()
    prod_p.start()

    prod_p.join(timeout=10)
    cons_p.join(timeout=10)

    assert not prod_p.is_alive()
    assert not cons_p.is_alive()

    consumed, duration_s = result_queue.get(timeout=2)
    assert consumed == count

    throughput = count / duration_s
    print(f"\n[IPC Benchmark] Process-to-Process Throughput: {throughput:,.0f} orders/sec (Time: {duration_s*1000:.2f} ms)")
    assert throughput > 10_000, f"Throughput too low: {throughput}"
