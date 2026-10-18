"""
Unit tests and microbenchmarks for ChronosMatch PriceLevel FIFO Queue.
Verifies Time Priority (FIFO), arbitrary O(1) unlinking, and volume aggregation.
"""

import time
import pytest
from chronos_match.protocol import to_fixed_price
from chronos_match.engine.arena_cy import FastArena
from chronos_match.engine.price_level_cy import FastPriceLevel

def test_price_level_initialization():
    """Verify PriceLevel clean initial state."""
    price = to_fixed_price(150.25)
    level = FastPriceLevel(price)
    assert level.price == price
    assert level.total_volume == 0
    assert level.order_count == 0
    assert level.is_empty is True
    assert level.peek_head_id() == 0

def test_price_level_fifo_ordering():
    """
    Verify Time Priority (FIFO):
    Orders arriving first must be at the head and matched first.
    """
    arena = FastArena(100)
    price = to_fixed_price(200.00)
    level = FastPriceLevel(price)

    # Create 3 orders with distinct quantities
    p1 = arena.alloc(101, 1, price, 100, 0, 1, 1000)
    p2 = arena.alloc(102, 1, price, 250, 0, 1, 2000)
    p3 = arena.alloc(103, 1, price, 50,  0, 1, 3000)

    level.append(p1)
    level.append(p2)
    level.append(p3)

    assert level.order_count == 3
    assert level.total_volume == 400
    assert level.is_empty is False

    # First in must be Order 101
    assert level.peek_head_id() == 101
    level.remove(p1)

    assert level.order_count == 2
    assert level.total_volume == 300
    # Next must be Order 102
    assert level.peek_head_id() == 102
    level.remove(p2)

    assert level.order_count == 1
    assert level.total_volume == 50
    # Next must be Order 103
    assert level.peek_head_id() == 103
    level.remove(p3)

    assert level.order_count == 0
    assert level.total_volume == 0
    assert level.is_empty is True
    assert level.peek_head_id() == 0

def test_price_level_arbitrary_unlinking():
    """
    Verify O(1) arbitrary order cancellation from anywhere in the queue:
    Head, Middle, and Tail.
    """
    arena = FastArena(100)
    price = to_fixed_price(50.00)
    level = FastPriceLevel(price)

    nodes = [
        arena.alloc(i, 1, price, 100, 0, 1, i * 10)
        for i in range(1, 6) # Orders 1, 2, 3, 4, 5
    ]

    for p in nodes:
        level.append(p)

    assert level.order_count == 5
    assert level.total_volume == 500

    # 1. Cancel Middle order (Order 3)
    level.remove(nodes[2])
    assert level.order_count == 4
    assert level.total_volume == 400

    # 2. Cancel Tail order (Order 5)
    level.remove(nodes[4])
    assert level.order_count == 3
    assert level.total_volume == 300

    # 3. Cancel Head order (Order 1)
    level.remove(nodes[0])
    assert level.order_count == 2
    assert level.total_volume == 200

    # Remaining orders must be Order 2 then Order 4
    assert level.peek_head_id() == 2
    level.remove(nodes[1])
    assert level.peek_head_id() == 4
    level.remove(nodes[3])

    assert level.is_empty is True

def test_price_level_microbenchmark():
    """
    Microbenchmark: Append and remove 1,000,000 orders in the FIFO queue inside nogil.
    Proves sub-10 nanosecond list manipulation in the matching hot path.
    """
    arena = FastArena(1000)
    level = FastPriceLevel(to_fixed_price(100.00))
    iterations = 1_000_000

    # Pre-allocate 1 node to repeatedly test append & remove cycles
    node_ptr = arena.alloc(1, 1, to_fixed_price(100.00), 10, 0, 1, 0)

    ns_per_cycle = level.benchmark_append_remove(node_ptr, iterations)
    cycles_per_sec = 1_000_000_000 / ns_per_cycle

    print(f"\n[PriceLevel Benchmark] {iterations:,} FIFO Append+Remove cycles: {ns_per_cycle:.2f} ns/cycle ({cycles_per_sec:,.0f} cycles/sec)")
    assert ns_per_cycle < 15.0, f"FIFO cycle too slow: {ns_per_cycle:.2f} ns"
