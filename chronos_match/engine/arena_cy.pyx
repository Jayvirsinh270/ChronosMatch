# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True

cimport cython
from libc.stdint cimport uint64_t, int64_t, uint32_t, uint16_t, uint8_t
from libc.stddef cimport size_t
import time

cdef extern from "order_node.h" nogil:
    ctypedef struct OrderNode:
        OrderNode* prev
        OrderNode* next
        uint64_t   order_id
        uint64_t   client_id
        int64_t    price
        uint32_t   quantity
        uint16_t   symbol_id
        uint8_t    side
        uint8_t    order_type
        uint64_t   timestamp_in_ns

cdef extern from "arena.h" nogil:
    ctypedef struct OrderArena:
        OrderNode* pool
        OrderNode* free_head
        size_t     capacity
        size_t     allocated_count
        size_t     peak_count

    OrderArena* arena_create(size_t capacity)
    void arena_destroy(OrderArena* arena)
    OrderNode* arena_alloc(OrderArena* arena)
    void arena_free(OrderArena* arena, OrderNode* node)
    void arena_reset(OrderArena* arena)

cdef class FastArena:
    """
    Cython wrapper around pure-C OrderArena.
    Provides memory-pooled node allocation with zero malloc during trading.
    """
    cdef OrderArena* _arena

    def __cinit__(self, size_t capacity):
        self._arena = arena_create(capacity)
        if self._arena == NULL:
            raise MemoryError(f"Failed to allocate OrderArena with capacity {capacity}")

    def __dealloc__(self):
        if self._arena != NULL:
            arena_destroy(self._arena)
            self._arena = NULL

    @property
    def capacity(self) -> int:
        return self._arena.capacity if self._arena != NULL else 0

    @property
    def allocated_count(self) -> int:
        return self._arena.allocated_count if self._arena != NULL else 0

    @property
    def peak_count(self) -> int:
        return self._arena.peak_count if self._arena != NULL else 0

    def alloc(
        self,
        uint64_t order_id,
        uint64_t client_id,
        int64_t price,
        uint32_t quantity,
        uint8_t side,
        uint16_t symbol_id,
        uint64_t timestamp_in_ns
    ) -> size_t:
        """
        Allocate a node from the arena and initialize order fields.
        Returns the memory address of the allocated OrderNode (or 0 if full).
        """
        cdef OrderNode* node
        with nogil:
            node = arena_alloc(self._arena)
        if node == NULL:
            return 0

        node.order_id = order_id
        node.client_id = client_id
        node.price = price
        node.quantity = quantity
        node.side = side
        node.order_type = 0 # LIMIT
        node.symbol_id = symbol_id
        node.timestamp_in_ns = timestamp_in_ns

        return <size_t>node

    def get_order_data(self, size_t node_ptr) -> dict:
        """Inspect order data from a node pointer."""
        cdef OrderNode* node = <OrderNode*>node_ptr
        if node == NULL:
            return {}
        return {
            "order_id": node.order_id,
            "client_id": node.client_id,
            "price": node.price,
            "quantity": node.quantity,
            "side": node.side,
            "symbol_id": node.symbol_id,
            "timestamp_in_ns": node.timestamp_in_ns,
        }

    def free(self, size_t node_ptr) -> None:
        """Return a node back to the arena free-list."""
        cdef OrderNode* node = <OrderNode*>node_ptr
        if node != NULL:
            with nogil:
                arena_free(self._arena, node)

    def reset(self) -> None:
        """Recycle all nodes back to free-list."""
        if self._arena != NULL:
            arena_reset(self._arena)

    def benchmark_alloc_free(self, size_t count) -> float:
        """
        Measure raw C-level allocation + deallocation time per operation in nanoseconds.
        Runs 100% inside nogil.
        """
        cdef size_t i
        cdef OrderNode* node
        cdef uint64_t t0, t1
        cdef OrderArena* arena = self._arena

        t0 = time.perf_counter_ns()
        with nogil:
            for i in range(count):
                node = arena_alloc(arena)
                if node != NULL:
                    arena_free(arena, node)
        t1 = time.perf_counter_ns()

        cdef double elapsed_ns = <double>(t1 - t0)
        return elapsed_ns / (<double>count) # Average ns per alloc+free pair
