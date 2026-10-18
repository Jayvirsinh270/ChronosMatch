"""
ChronosMatch: Wire-to-Engine Integration Bridge.
Connects the shared memory input ring buffer directly to the Cython Limit Order Book.
Measures end-to-end wire-to-match latency down to the nanosecond and forwards
matched execution records to the outbound trade ring buffer.
"""

import time
from typing import Optional, List, Dict, Tuple

from chronos_match.protocol import (
    OrderPacket,
    TradePacket,
    OrderType,
    to_fixed_price,
)
from chronos_match.ring_buffer import RingBuffer
from chronos_match.engine.order_book_cy import FastOrderBook
from chronos_match.monitor.metrics import LatencyProfiler, LatencyStats

class EngineWorker:
    """
    High-throughput pipeline worker bridging shared memory IPC to the matching engine.
    """
    def __init__(
        self,
        in_buffer: RingBuffer,
        order_book: Optional[FastOrderBook] = None,
        out_trade_buffer: Optional[RingBuffer] = None,
        profiler: Optional[LatencyProfiler] = None,
        arena_capacity: int = 100_000
    ):
        self.in_buffer = in_buffer
        self.out_trade_buffer = out_trade_buffer
        self.book = order_book or FastOrderBook(arena_capacity=arena_capacity)
        self.profiler = profiler or LatencyProfiler(window_size=100_000)

        # Performance counters
        self.orders_processed = 0
        self.cancellations_processed = 0
        self.trades_generated = 0
        self.shares_traded = 0

    def process_next_order(self) -> bool:
        """
        Poll and process a single order from the input ring buffer.
        Returns True if an order was read and executed, False if buffer was empty.
        """
        order = self.in_buffer.read_order()
        if order is None:
            return False

        t_matched = time.perf_counter_ns()
        self.orders_processed += 1

        # 1. CANCELLATION REQUEST
        if order.order_type == int(OrderType.CANCEL):
            cancelled = self.book.cancel_order(order.order_id)
            if cancelled:
                self.cancellations_processed += 1
            # Record wire-to-cancel latency
            if order.timestamp_in_ns > 0:
                self.profiler.record_delta(order.timestamp_in_ns, t_matched)
            return True

        # 2. LIMIT / MARKET ORDER MATCHING
        matches = self.book.add_limit_order(
            order_id=order.order_id,
            client_id=order.client_id,
            price=order.price,
            quantity=order.quantity,
            side=order.side,
            symbol_id=order.symbol_id,
            timestamp_ns=order.timestamp_in_ns
        )

        # Record end-to-end latency and forward trades to outbound buffer
        if order.timestamp_in_ns > 0:
            self.profiler.record_delta(order.timestamp_in_ns, t_matched)

        if matches:
            for m in matches:
                self.trades_generated += 1
                self.shares_traded += m["quantity"]

                # Push to outbound trade buffer if attached
                if self.out_trade_buffer is not None:
                    latency_ns = t_matched - order.timestamp_in_ns if order.timestamp_in_ns > 0 else 0
                    trade_packet = TradePacket(
                        trade_id=m["trade_id"],
                        buy_order_id=m["buy_order_id"],
                        sell_order_id=m["sell_order_id"],
                        price=m["execution_price"],
                        quantity=m["quantity"],
                        symbol_id=order.symbol_id,
                        aggressor_side=m["aggressor_side"],
                        match_timestamp_ns=t_matched,
                        latency_ns=latency_ns
                    )
                    self.out_trade_buffer.write_trade(trade_packet)

        return True

    def drain_all_available(self, max_batch: int = 10_000) -> int:
        """
        Drain all currently available orders from the input buffer up to max_batch.
        Returns count of orders processed.
        """
        count = 0
        while count < max_batch:
            if not self.process_next_order():
                break
            count += 1
        return count

    def get_summary(self) -> Dict:
        """Return operational throughput and latency statistics."""
        stats = self.profiler.compute_stats()
        return {
            "orders_processed": self.orders_processed,
            "cancellations_processed": self.cancellations_processed,
            "trades_generated": self.trades_generated,
            "shares_traded": self.shares_traded,
            "best_bid": self.book.best_bid_price,
            "best_ask": self.book.best_ask_price,
            "spread": self.book.spread,
            "latency": stats._asdict() if stats else None,
            "histogram": self.profiler.compute_histogram()
        }
