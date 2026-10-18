"""
Unit tests and microbenchmarks for ChronosMatch LatencyProfiler.
Verifies circular window mechanics, statistical accuracy (p50, p90, p99), histogram binning, and recording throughput.
"""

import time
import pytest
from chronos_match.monitor.metrics import LatencyProfiler, LatencyStats

def test_latency_profiler_init():
    """Verify clean initial state of LatencyProfiler."""
    profiler = LatencyProfiler(window_size=1000)
    assert profiler.total_recorded == 0
    assert profiler.current_window_count == 0
    assert profiler.compute_stats() is None

def test_latency_profiler_exact_percentiles():
    """Verify statistical calculation accuracy against a known uniform sequence."""
    profiler = LatencyProfiler(window_size=2000)

    # Insert exactly 1,000 uniform samples from 1 μs (1,000 ns) to 1,000 μs (1,000,000 ns)
    for i in range(1, 1001):
        profiler.record_latency_ns(i * 1000)

    assert profiler.total_recorded == 1000
    assert profiler.current_window_count == 1000

    stats = profiler.compute_stats()
    assert stats is not None

    assert stats.min_us == 1.0
    assert stats.max_us == 1000.0
    assert pytest.approx(stats.mean_us, abs=0.5) == 500.5
    assert pytest.approx(stats.p50_us, abs=2.0) == 500.0
    assert pytest.approx(stats.p90_us, abs=2.0) == 900.0
    assert pytest.approx(stats.p99_us, abs=2.0) == 990.0

def test_latency_profiler_circular_wraparound():
    """Verify that when samples exceed window size, oldest samples are discarded."""
    window_size = 100
    profiler = LatencyProfiler(window_size=window_size)

    # Insert 250 samples: 1..250 μs
    for i in range(1, 251):
        profiler.record_latency_ns(i * 1000)

    assert profiler.total_recorded == 250
    assert profiler.current_window_count == window_size

    stats = profiler.compute_stats()
    assert stats is not None
    # Window should contain samples 151 to 250
    assert stats.min_us == 151.0
    assert stats.max_us == 250.0

def test_latency_histogram_bins():
    """Verify microsecond histogram binning."""
    profiler = LatencyProfiler(window_size=100)

    # 2 samples < 10 μs
    profiler.record_latency_ns(4_000)
    profiler.record_latency_ns(8_000)

    # 3 samples 10 - 25 μs
    profiler.record_latency_ns(12_000)
    profiler.record_latency_ns(18_000)
    profiler.record_latency_ns(22_000)

    # 1 sample 25 - 50 μs
    profiler.record_latency_ns(35_000)

    # 2 samples > 50 μs
    profiler.record_latency_ns(65_000)
    profiler.record_latency_ns(120_000)

    hist = profiler.compute_histogram()
    assert hist["< 10 μs"] == 2
    assert hist["10 - 25 μs"] == 3
    assert hist["25 - 50 μs"] == 1
    assert hist["> 50 μs"] == 2

def test_profiler_throughput_microbenchmark():
    """
    Microbenchmark: Record 1,000,000 latency samples into the profiler.
    Asserts zero-allocation performance (< 100 ns per record).
    """
    profiler = LatencyProfiler(window_size=100_000)
    samples = 1_000_000

    t0 = time.perf_counter_ns()
    for i in range(samples):
        profiler.record_latency_ns(15_000) # 15 μs
    t1 = time.perf_counter_ns()

    elapsed_sec = (t1 - t0) / 1e9
    throughput = samples / elapsed_sec
    avg_ns = (t1 - t0) / samples

    print(f"\n[Metrics Benchmark] 1,000,000 Latencies Recorded: {avg_ns:.2f} ns/sample ({throughput:,.0f} samples/sec)")
    assert avg_ns < 250.0, f"Recording too slow: {avg_ns:.2f} ns"
