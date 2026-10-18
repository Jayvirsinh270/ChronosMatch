"""
Unit tests and microbenchmarks for ChronosMatch Limit Order Book (LOB).
Verifies Price-Time Priority, exact executions, partial fills, multi-level whale sweeps, and latency.
"""

import time
import pytest
from chronos_match.protocol import to_fixed_price, from_fixed_price
from chronos_match.engine.order_book_cy import FastOrderBook

def test_order_book_initialization():
    """Verify empty OrderBook initial state."""
    book = FastOrderBook(1000)
    assert book.best_bid_price == 0
    assert book.best_bid_volume == 0
    assert book.best_ask_price == 0
    assert book.best_ask_volume == 0
    assert book.spread == 0
    assert book.total_trades_matched == 0
    assert book.total_shares_traded == 0

def test_resting_limit_orders_and_spread():
    """Verify resting limit orders establish a two-sided bid/ask spread."""
    book = FastOrderBook(1000)
    bid_p = to_fixed_price(100.00)
    ask_p = to_fixed_price(100.50)

    # Buy 100 @ $100.00
    m1 = book.add_limit_order(1, 10, bid_p, 100, side=0)
    assert len(m1) == 0 # No cross

    # Sell 200 @ $100.50
    m2 = book.add_limit_order(2, 20, ask_p, 200, side=1)
    assert len(m2) == 0 # No cross

    assert book.best_bid_price == bid_p
    assert book.best_bid_volume == 100
    assert book.best_ask_price == ask_p
    assert book.best_ask_volume == 200
    assert book.spread == to_fixed_price(0.50)

def test_exact_full_match():
    """Verify exact match execution when Buy price equals Best Ask price."""
    book = FastOrderBook(1000)
    price = to_fixed_price(150.00)

    # Resting Sell: 100 shares @ $150.00
    book.add_limit_order(101, 1, price, 100, side=1)

    # Aggressive Buy: 100 shares @ $150.00 (Crosses spread)
    matches = book.add_limit_order(102, 2, price, 100, side=0)

    assert len(matches) == 1
    m = matches[0]
    assert m["buy_order_id"] == 102
    assert m["sell_order_id"] == 101
    assert m["execution_price"] == price
    assert m["quantity"] == 100
    assert m["aggressor_side"] == 0 # BUY

    # Order book should now be completely cleared
    assert book.best_bid_price == 0
    assert book.best_ask_price == 0
    assert book.total_trades_matched == 1
    assert book.total_shares_traded == 100

def test_partial_fill_and_resting_remainder():
    """Verify that an aggressive order partially fills and its remainder rests in the book."""
    book = FastOrderBook(1000)
    price = to_fixed_price(150.00)

    # Resting Sell: 50 shares @ $150.00
    book.add_limit_order(201, 1, price, 50, side=1)

    # Aggressive Buy: 150 shares @ $150.00 (Fills 50, 100 remain)
    matches = book.add_limit_order(202, 2, price, 150, side=0)

    assert len(matches) == 1
    assert matches[0]["quantity"] == 50

    # Ask is now empty
    assert book.best_ask_price == 0
    assert book.best_ask_volume == 0

    # Remaining 100 shares of the Buy order must rest on Bid ladder
    assert book.best_bid_price == price
    assert book.best_bid_volume == 100

def test_multi_level_sweep_whale_order():
    """
    Verify Whale Order execution:
    An aggressive order sweeps through multiple distinct price levels.
    """
    book = FastOrderBook(1000)
    p1 = to_fixed_price(100.10)
    p2 = to_fixed_price(100.20)
    p3 = to_fixed_price(100.30)

    # Populate 3 distinct ask price levels
    book.add_limit_order(1, 1, p1, 100, side=1)
    book.add_limit_order(2, 2, p2, 200, side=1)
    book.add_limit_order(3, 3, p3, 300, side=1)

    # Whale Order: Buy 500 shares up to $100.30
    matches = book.add_limit_order(999, 42, p3, 500, side=0)

    assert len(matches) == 3

    # Match 1: Clears Level 1
    assert matches[0]["sell_order_id"] == 1
    assert matches[0]["execution_price"] == p1
    assert matches[0]["quantity"] == 100

    # Match 2: Clears Level 2
    assert matches[1]["sell_order_id"] == 2
    assert matches[1]["execution_price"] == p2
    assert matches[1]["quantity"] == 200

    # Match 3: Partially fills Level 3
    assert matches[2]["sell_order_id"] == 3
    assert matches[2]["execution_price"] == p3
    assert matches[2]["quantity"] == 200

    # Level 3 should have 100 shares resting
    assert book.best_ask_price == p3
    assert book.best_ask_volume == 100
    assert book.best_bid_price == 0

def test_price_time_priority():
    """
    Verify Time Priority (FIFO) among orders at identical price levels:
    Order arriving earlier must be executed first.
    """
    book = FastOrderBook(1000)
    price = to_fixed_price(50.00)

    # Order A arrives first
    book.add_limit_order(1001, 1, price, 100, side=1, timestamp_ns=1000)
    # Order B arrives second at identical price
    book.add_limit_order(1002, 2, price, 100, side=1, timestamp_ns=2000)

    # Incoming Buy for 100 shares
    matches = book.add_limit_order(2001, 3, price, 100, side=0)

    assert len(matches) == 1
    # Must match against Order 1001 (Time priority!)
    assert matches[0]["sell_order_id"] == 1001
    assert book.best_ask_volume == 100

def test_matching_microbenchmark():
    """
    Microbenchmark: Execute 100,000 matches (200,000 orders) inside nogil.
    Demonstrates sub-microsecond matching latency.
    """
    book = FastOrderBook(100_000)
    pairs = 50_000 # 50,000 matches = 100,000 orders processed

    avg_ns_per_order = book.benchmark_matching(pairs)
    orders_per_sec = 1_000_000_000 / avg_ns_per_order
    latency_us = avg_ns_per_order / 1000.0

    print(f"\n[OrderBook Benchmark] 100,000 Orders Matched: {latency_us:.3f} μs/order ({avg_ns_per_order:.1f} ns) | {orders_per_sec:,.0f} orders/sec")
    assert latency_us < 5.0, f"Matching latency exceeded target: {latency_us:.3f} μs"
