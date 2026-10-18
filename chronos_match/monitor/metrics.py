"""
ChronosMatch: High-Resolution Nanosecond Latency Profiler.
Collects wire-to-match execution latencies and computes empirical percentiles:
- Min, Max, Mean
- Median (p50)
- Tail Latencies (p90, p99, p99.9)
- Microsecond histogram distribution
"""

import array
import time
from typing import NamedTuple, Dict, Optional, List

class LatencyStats(NamedTuple):
    sample_count: int
    min_us: float
    max_us: float
    mean_us: float
    p50_us: float
    p90_us: float
    p99_us: float
    p999_us: float

class LatencyProfiler:
    """
    Fixed-memory circular buffer latency profiler.
    Stores raw nanosecond latency samples with zero dynamic allocation overhead.
    """
    def __init__(self, window_size: int = 100_000):
        self.window_size = window_size
        # Unsigned 64-bit integer contiguous array (low memory footprint: ~800 KB for 100k samples)
        self._samples = array.array('Q', [0] * window_size)
        self._head = 0
        self._count = 0
        self._total_recorded = 0

    def record_latency_ns(self, latency_ns: int) -> None:
        """Record an end-to-end latency delta in nanoseconds."""
        self._samples[self._head] = latency_ns
        self._head += 1
        if self._head == self.window_size:
            self._head = 0
        if self._count < self.window_size:
            self._count += 1
        self._total_recorded += 1

    def record_delta(self, start_ns: int, end_ns: int) -> None:
        """Record latency delta calculated from two hardware timestamps."""
        delta = end_ns - start_ns if end_ns >= start_ns else 0
        self.record_latency_ns(delta)

    @property
    def total_recorded(self) -> int:
        return self._total_recorded

    @property
    def current_window_count(self) -> int:
        return self._count

    def compute_stats(self) -> Optional[LatencyStats]:
        """
        Compute empirical percentile statistics over the active sliding window.
        Returns LatencyStats in microseconds (μs), or None if no samples exist.
        """
        if self._count == 0:
            return None

        # Extract active window slice and sort for percentile calculation
        active = self._samples[:self._count]
        sorted_samples = sorted(active)
        n = len(sorted_samples)

        min_ns = sorted_samples[0]
        max_ns = sorted_samples[-1]
        sum_ns = sum(sorted_samples)
        mean_ns = sum_ns / n

        p50_idx = min(int(n * 0.50), n - 1)
        p90_idx = min(int(n * 0.90), n - 1)
        p99_idx = min(int(n * 0.99), n - 1)
        p999_idx = min(int(n * 0.999), n - 1)

        return LatencyStats(
            sample_count=n,
            min_us=min_ns / 1000.0,
            max_us=max_ns / 1000.0,
            mean_us=mean_ns / 1000.0,
            p50_us=sorted_samples[p50_idx] / 1000.0,
            p90_us=sorted_samples[p90_idx] / 1000.0,
            p99_us=sorted_samples[p99_idx] / 1000.0,
            p999_us=sorted_samples[p999_idx] / 1000.0,
        )

    def compute_histogram(self) -> Dict[str, int]:
        """
        Group active latencies into standard HFT microsecond bins:
        - < 10 μs (Ultra Fast)
        - 10-25 μs (Target Range)
        - 25-50 μs (Acceptable)
        - > 50 μs (SLA Breach / Tail Jitter)
        """
        bins = {
            "< 10 μs": 0,
            "10 - 25 μs": 0,
            "25 - 50 μs": 0,
            "> 50 μs": 0,
        }
        if self._count == 0:
            return bins

        for i in range(self._count):
            us = self._samples[i] / 1000.0
            if us < 10.0:
                bins["< 10 μs"] += 1
            elif us < 25.0:
                bins["10 - 25 μs"] += 1
            elif us <= 50.0:
                bins["25 - 50 μs"] += 1
            else:
                bins["> 50 μs"] += 1

        return bins

    def reset(self) -> None:
        """Clear active samples window."""
        self._head = 0
        self._count = 0
        self._total_recorded = 0
