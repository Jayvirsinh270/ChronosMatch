"""
ChronosMatch: High-Throughput Asyncio Market Firehose.
Blasts simulated financial tick data into shared memory or stream handlers.
Capable of sustaining 100,000+ orders/second.
"""

import asyncio
import time
from typing import Optional, List, Callable, Tuple

from chronos_match.protocol import OrderPacket
from chronos_match.market.distribution import OrderDistributionGenerator
from chronos_match.ring_buffer import RingBuffer

class AsyncMarketFirehose:
    """
    High-throughput asynchronous tick generator simulating exchange market data feeds.
    """
    def __init__(
        self,
        symbol_id: int = 1,
        initial_price: float = 150.00,
        distribution_generator: Optional[OrderDistributionGenerator] = None
    ):
        self.generator = distribution_generator or OrderDistributionGenerator(
            symbol_id=symbol_id,
            initial_price=initial_price
        )
        self.total_generated = 0

    def generate_batch(self, count: int) -> List[OrderPacket]:
        """Generate a batch of OrderPackets in-memory."""
        batch = []
        for _ in range(count):
            order = self.generator.generate_next_order()
            batch.append(order)
            self.total_generated += 1
        return batch

    async def stream_to_ring_buffer(
        self,
        ring_buffer: RingBuffer,
        total_orders: int,
        batch_size: int = 500,
        yield_interval: int = 2000
    ) -> Tuple[int, float]:
        """
        Stream orders directly into the shared memory SPSC RingBuffer.
        Handles producer backpressure using cooperative yielding.
        Returns (orders_written, elapsed_seconds).
        """
        written = 0
        t0 = time.perf_counter_ns()

        while written < total_orders and ring_buffer.is_active:
            chunk = min(batch_size, total_orders - written)
            for _ in range(chunk):
                order = self.generator.generate_next_order()
                # Busy-spin with cooperative yield if buffer is full
                while not ring_buffer.write_order(order):
                    if not ring_buffer.is_active:
                        break
                    await asyncio.sleep(0.0001) # Yield to event loop on backpressure

                written += 1
                self.total_generated += 1

            if written % yield_interval == 0:
                await asyncio.sleep(0) # Cooperative event loop yield

        t1 = time.perf_counter_ns()
        elapsed_sec = (t1 - t0) / 1e9
        return written, elapsed_sec

    def sync_blast_to_ring_buffer(
        self,
        ring_buffer: RingBuffer,
        total_orders: int
    ) -> Tuple[int, float]:
        """
        Synchronous zero-overhead raw blast into ring buffer.
        Maximizes throughput without asyncio scheduling overhead.
        """
        written = 0
        t0 = time.perf_counter_ns()

        for _ in range(total_orders):
            if not ring_buffer.is_active:
                break
            order = self.generator.generate_next_order()
            while not ring_buffer.write_order(order):
                if not ring_buffer.is_active:
                    break
            written += 1
            self.total_generated += 1

        t1 = time.perf_counter_ns()
        elapsed_sec = (t1 - t0) / 1e9
        return written, elapsed_sec
