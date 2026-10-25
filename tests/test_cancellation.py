"""
Unit tests and microbenchmarks for ChronosMatch O(1) Order Cancellation.
Verifies direct hash map indexing, arbitrary unlinking, filled-order immunity, and sub-microsecond cancellation latency.
"""

import time
import pytest
from chronos_match.protocol import to_fixed_price
from chronos_match.engine.order_book_cy import FastOrderBook

def test_cancel_resting_bid_order():
    """Verify successful cancellation of a resting limit buy order."""
    book = FastOrderBook(1000)
    price = to_fixed_price(100.00)
    order_id = 42

    book.add_limit_order(order_id, 1, price, 100, side=0)
    assert book.best_bid_price == price
    assert book.best_bid_volume == 100

    # Cancel order
    cancelled = book.cancel_order(order_id)
    assert cancelled is True

    # Book must now be empty
    assert book.best_bid_price == 0
    assert book.best_bid_volume == 0

def test_cancel_resting_ask_order():
    """Verify successful cancellation of a resting limit sell order."""
    book = FastOrderBook(1000)
    price = to_fixed_price(200.00)
    order_id = 99

    book.add_limit_order(order_id, 1, price, 250, side=1)
    assert book.best_ask_price == price
    assert book.best_ask_volume == 250

    # Cancel order
    cancelled = book.cancel_order(order_id)
    assert cancelled is True

    assert book.best_ask_price == 0
    assert book.best_ask_volume == 0

def test_cancel_nonexistent_order():
    """Verify attempting to cancel an unknown order returns False safely."""
    book = FastOrderBook(1000)
    assert book.cancel_order(999999) is False

def test_cancel_already_filled_order():
    """Verify that an order which has already executed cannot be cancelled."""
    book = FastOrderBook(1000)
    price = to_fixed_price(150.00)
    sell_id = 101
    buy_id = 102

    # Place Ask
    book.add_limit_order(sell_id, 1, price, 100, side=1)
    # Fill Ask completely with aggressive Buy
    matches = book.add_limit_order(buy_id, 2, price, 100, side=0)
    assert len(matches) == 1

    # Attempting to cancel executed Ask must fail
    assert book.cancel_order(sell_id) is False
    # Attempting to cancel executed Buy must fail
    assert book.cancel_order(buy_id) is False

def test_cancel_middle_order_at_identical_price():
    """
    Verify cancelling an order positioned in the middle of a FIFO price level
    preserves correct execution sequence of surrounding orders.
    """
    book = FastOrderBook(1000)
    price = to_fixed_price(75.00)

    # Place 3 orders at identical price
    book.add_limit_order(1, 1, price, 100, side=1) # Head
    book.add_limit_order(2, 2, price, 200, side=1) # Middle
    book.add_limit_order(3, 3, price, 300, side=1) # Tail

    assert book.best_ask_volume == 600

    # Cancel Order 2 (Middle)
    assert book.cancel_order(2) is True
    assert book.best_ask_volume == 400

    # Incoming Buy of 100 shares must fill Order 1
    m1 = book.add_limit_order(10, 4, price, 100, side=0)
    assert len(m1) == 1
    assert m1[0]["sell_order_id"] == 1
    assert book.best_ask_volume == 300

    # Incoming Buy of 300 shares must fill Order 3 (Order 2 was cleanly unlinked)
    m2 = book.add_limit_order(11, 4, price, 300, side=0)
    assert len(m2) == 1
    assert m2[0]["sell_order_id"] == 3
    assert book.best_ask_volume == 0

def test_cancellation_microbenchmark():
    """
    Microbenchmark: Place and cancel 20,000 resting orders in nogil.
    Measures pure O(1) hash table lookup + linked-list unlinking latency.
    """
    book = FastOrderBook(100_000)
    count = 20_000

    avg_ns = book.benchmark_cancellations(count)
    cancels_per_sec = 1_000_000_000 / avg_ns

    print(f"\n[Cancellation Benchmark] {count:,} O(1) Cancellations: {avg_ns:.2f} ns/cancel ({cancels_per_sec:,.0f} cancels/sec)")
    # Allow headroom for Windows thread scheduling jitter during full test suites
    assert avg_ns < 150.0, f"Cancellation too slow: {avg_ns:.2f} ns"
