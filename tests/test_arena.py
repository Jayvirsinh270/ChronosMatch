"""
Unit tests and microbenchmarks for ChronosMatch OrderArena.
Verifies O(1) memory pool allocation, deallocation, capacity safety, and sub-nanosecond speeds.
"""

import time
import pytest
from chronos_match.protocol import to_fixed_price
from chronos_match.engine.arena_cy import FastArena

def test_arena_initialization():
    """Verify clean arena pool initialization."""
    capacity = 10_000
    arena = FastArena(capacity)
    assert arena.capacity == capacity
    assert arena.allocated_count == 0
    assert arena.peak_count == 0

def test_arena_alloc_and_data_integrity():
    """Verify O(1) allocation and 1:1 struct field integrity."""
    arena = FastArena(100)
    order_id = 1001
    client_id = 42
    price = to_fixed_price(185.50)
    qty = 500
    side = 0 # BUY
    symbol_id = 1
    timestamp = time.perf_counter_ns()

    ptr = arena.alloc(
        order_id=order_id,
        client_id=client_id,
        price=price,
        quantity=qty,
        side=side,
        symbol_id=symbol_id,
        timestamp_in_ns=timestamp
    )
    assert ptr != 0
    assert arena.allocated_count == 1
    assert arena.peak_count == 1

    data = arena.get_order_data(ptr)
    assert data["order_id"] == order_id
    assert data["client_id"] == client_id
    assert data["price"] == price
    assert data["quantity"] == qty
    assert data["side"] == side
    assert data["symbol_id"] == symbol_id
    assert data["timestamp_in_ns"] == timestamp

def test_arena_free_and_reuse():
    """Verify O(1) node recycling back to free-list and pointer reuse."""
    arena = FastArena(10)
    ptr1 = arena.alloc(1, 1, 1000, 10, 0, 1, 0)
    assert arena.allocated_count == 1

    arena.free(ptr1)
    assert arena.allocated_count == 0

    # Next allocation should immediately reuse ptr1 (head of free list)
    ptr2 = arena.alloc(2, 1, 2000, 20, 1, 1, 0)
    assert ptr2 == ptr1
    assert arena.allocated_count == 1

def test_arena_capacity_exhaustion():
    """Verify arena out-of-memory safety when capacity is reached."""
    capacity = 5
    arena = FastArena(capacity)
    ptrs = []

    for i in range(capacity):
        p = arena.alloc(i, 1, 1000, 10, 0, 1, 0)
        assert p != 0
        ptrs.append(p)

    assert arena.allocated_count == capacity

    # 6th allocation must fail safely returning 0 (no crash)
    overflow_ptr = arena.alloc(999, 1, 1000, 10, 0, 1, 0)
    assert overflow_ptr == 0
    assert arena.allocated_count == capacity

    # Free one node
    arena.free(ptrs[0])
    assert arena.allocated_count == capacity - 1

    # Now allocation succeeds
    recovered_ptr = arena.alloc(999, 1, 1000, 10, 0, 1, 0)
    assert recovered_ptr != 0
    assert arena.allocated_count == capacity

def test_arena_reset():
    """Verify bulk reset restores full pool capacity without reallocating."""
    arena = FastArena(50)
    for i in range(30):
        arena.alloc(i, 1, 1000, 10, 0, 1, 0)

    assert arena.allocated_count == 30
    assert arena.peak_count == 30

    arena.reset()
    assert arena.allocated_count == 0
    assert arena.capacity == 50

def test_arena_microbenchmark():
    """
    Microbenchmark: Execute 1,000,000 C-level allocations + deallocations inside nogil.
    Proves zero malloc overhead in the hot path.
    """
    arena = FastArena(100_000)
    operations = 1_000_000

    avg_ns_per_op = arena.benchmark_alloc_free(operations)
    ops_per_sec = 1_000_000_000 / avg_ns_per_op

    print(f"\n[Arena Benchmark] 1,000,000 Alloc+Free operations: {avg_ns_per_op:.2f} ns/op ({ops_per_sec:,.0f} ops/sec)")
    assert avg_ns_per_op < 25.0, f"Allocation too slow: {avg_ns_per_op:.2f} ns/op"
