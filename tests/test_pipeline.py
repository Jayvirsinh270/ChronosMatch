"""
End-to-End integration tests and SLA latency audits for ChronosMatch.
Verifies the complete pipeline: Market Firehose -> Shared Memory IPC -> Cython Matching Engine -> Outbound Trade Buffer.
"""

import time
import pytest
from pathlib import Path

from chronos_match.protocol import to_fixed_price
from chronos_match.ring_buffer import RingBuffer
from chronos_match.market.firehose import AsyncMarketFirehose
from chronos_match.market.bridge import EngineWorker
from chronos_match.monitor.metrics import LatencyProfiler

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

def test_pipeline_single_process_flow():
    """Verify orders written by firehose are matched by EngineWorker and emitted to trade buffer."""
    in_path = TEST_DIR / "pipe_in.ring"
    out_path = TEST_DIR / "pipe_out.ring"

    in_rb = RingBuffer(str(in_path), capacity=1024, create=True)
    out_rb = RingBuffer(str(out_path), capacity=1024, create=True)

    worker = EngineWorker(in_buffer=in_rb, out_trade_buffer=out_rb)
    firehose = AsyncMarketFirehose(initial_price=100.00)

    # Blast 500 orders into input buffer
    for _ in range(500):
        order = firehose.generator.generate_next_order()
        in_rb.write_order(order)

    # Worker drains buffer
    drained = worker.drain_all_available(max_batch=1000)
    assert drained == 500
    assert worker.orders_processed == 500

    # Summary audit
    summary = worker.get_summary()
    assert summary["orders_processed"] == 500
    assert summary["latency"] is not None

    in_rb.close()
    out_rb.close()

def test_end_to_end_latency_sla_audit():
    """
    Sub-50 Microsecond Latency SLA Audit:
    Streams 20,000 orders through shared memory into the Cython matching engine.
    Measures end-to-end wire-to-match latency and validates p99 < 50.0 μs.
    """
    in_path = TEST_DIR / "sla_in.ring"
    out_path = TEST_DIR / "sla_out.ring"

    in_rb = RingBuffer(str(in_path), capacity=8192, create=True)
    out_rb = RingBuffer(str(out_path), capacity=8192, create=True)

    profiler = LatencyProfiler(window_size=50_000)
    worker = EngineWorker(in_buffer=in_rb, out_trade_buffer=out_rb, profiler=profiler)
    firehose = AsyncMarketFirehose(initial_price=150.00)

    total_orders = 20_000
    t0 = time.perf_counter_ns()

    # Stream orders in real-time through shared memory into matching engine
    for _ in range(total_orders):
        t_ingress = time.perf_counter_ns()
        order = firehose.generator.generate_next_order(ingress_timestamp_ns=t_ingress)
        in_rb.write_order(order)
        worker.process_next_order()

    t1 = time.perf_counter_ns()
    elapsed_ms = (t1 - t0) / 1e6
    stats = profiler.compute_stats()

    assert stats is not None
    assert worker.orders_processed == total_orders

    print(f"\n" + "="*70)
    print(f"  CHRONOS-MATCH END-TO-END WIRE-TO-MATCH SLA AUDIT ({total_orders:,} Orders)")
    print(f"="*70)
    print(f"  Total Pipeline Time : {elapsed_ms:.2f} ms ({total_orders / (elapsed_ms / 1000):,.0f} orders/sec)")
    print(f"  Trades Executed     : {worker.trades_generated:,}")
    print(f"  Shares Traded       : {worker.shares_traded:,}")
    print(f"  Cancellations       : {worker.cancellations_processed:,}")
    print(f"  ------------------------------------------------------------------")
    print(f"  Min Execution Latency : {stats.min_us:.3f} μs")
    print(f"  Median (p50) Latency  : {stats.p50_us:.3f} μs")
    print(f"  p90 Latency           : {stats.p90_us:.3f} μs")
    print(f"  p99 Tail Latency      : {stats.p99_us:.3f} μs  <-- Target < 50.0 μs")
    print(f"  p99.9 Tail Latency    : {stats.p999_us:.3f} μs")
    print(f"="*70)

    # Core Project SLA Validation
    assert stats.p99_us < 50.0, f"SLA Breach: p99 latency exceeded 50 μs: {stats.p99_us:.3f} μs"
    assert stats.p50_us < 15.0, f"Median latency too high: {stats.p50_us:.3f} μs"

    in_rb.close()
    out_rb.close()
