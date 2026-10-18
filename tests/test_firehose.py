"""
Unit tests and throughput benchmarks for ChronosMatch Market Firehose.
Verifies stochastic distribution ratios, asyncio streaming, and 100k+ orders/sec throughput.
"""

import asyncio
import time
import pytest
from pathlib import Path

from chronos_match.protocol import OrderType, OrderSide
from chronos_match.market.distribution import OrderDistributionGenerator
from chronos_match.market.firehose import AsyncMarketFirehose
from chronos_match.ring_buffer import RingBuffer

TEST_DIR = Path("d:/infotech_internship_project/.test_shm")

@pytest.fixture(autouse=True)
def setup_teardown():
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    yield
    for f in TEST_DIR.glob("*.ring"):
        try:
            f.unlink()
        except Exception:
            pass

def test_distribution_generator_ratios():
    """Verify stochastic order generator generates expected distribution of orders."""
    gen = OrderDistributionGenerator(initial_price=100.00, seed=42)
    sample_size = 5_000

    cancels = 0
    buys = 0
    sells = 0

    for _ in range(sample_size):
        order = gen.generate_next_order()
        assert order.symbol_id == 1
        assert order.timestamp_in_ns > 0

        if order.order_type == int(OrderType.CANCEL):
            cancels += 1
        else:
            if order.side == int(OrderSide.BUY):
                buys += 1
            else:
                sells += 1
            assert order.price > 0
            assert order.quantity > 0

    # Verify cancellations were generated once resting pool accumulated
    assert cancels > 500, f"Expected cancels > 500, got {cancels}"
    # Verify two-sided market
    assert buys > 1000
    assert sells > 1000

def test_firehose_batch_generation():
    """Verify fast batch order generation."""
    firehose = AsyncMarketFirehose(initial_price=200.00)
    batch = firehose.generate_batch(10_000)

    assert len(batch) == 10_000
    assert firehose.total_generated == 10_000
    assert batch[0].order_id == 1

def test_firehose_stream_to_ring_buffer_async():
    """
    Verify asynchronous streaming into shared memory RingBuffer
    with concurrent background consumer.
    """
    rb_path = TEST_DIR / "firehose_test.ring"
    rb = RingBuffer(str(rb_path), capacity=4096, create=True)
    firehose = AsyncMarketFirehose(initial_price=150.00)

    total_orders = 10_000
    consumed = []

    async def consumer():
        while len(consumed) < total_orders:
            order = rb.read_order()
            if order is not None:
                consumed.append(order)
            else:
                await asyncio.sleep(0.0001)

    async def producer():
        written, elapsed = await firehose.stream_to_ring_buffer(rb, total_orders, batch_size=250)
        return written, elapsed

    async def main():
        c_task = asyncio.create_task(consumer())
        p_res = await producer()
        await c_task
        return p_res

    written, elapsed = asyncio.run(main())
    rb.close()

    assert written == total_orders
    assert len(consumed) == total_orders
    # Verify FIFO continuity
    assert consumed[0].order_id == 1

def test_firehose_throughput_benchmark():
    """
    Microbenchmark: Test blast generation of 100,000 orders.
    Verifies the market firehose satisfies the 100,000+ orders/sec specification.
    """
    firehose = AsyncMarketFirehose(initial_price=100.00)
    count = 100_000

    t0 = time.perf_counter_ns()
    batch = firehose.generate_batch(count)
    t1 = time.perf_counter_ns()

    duration_sec = (t1 - t0) / 1e9
    throughput = count / duration_sec

    print(f"\n[Firehose Benchmark] 100,000 Orders Generated: {throughput:,.0f} orders/sec (Time: {duration_sec*1000:.2f} ms)")
    assert throughput > 100_000, f"Firehose throughput too low: {throughput}"
