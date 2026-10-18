# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True

cimport cython
from libc.stdint cimport uint64_t, int64_t, uint32_t, uint16_t, uint8_t
from libc.stdlib cimport malloc, free
from libc.stddef cimport size_t

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

cdef extern from "price_level.h" nogil:
    ctypedef struct PriceLevel:
        int64_t     price
        uint64_t    total_volume
        uint32_t    order_count
        OrderNode*  head
        OrderNode*  tail
        PriceLevel* next_level
        PriceLevel* prev_level

    void price_level_init(PriceLevel* level, int64_t price)
    void price_level_append(PriceLevel* level, OrderNode* node)
    void price_level_remove(PriceLevel* level, OrderNode* node)
    OrderNode* price_level_peek(PriceLevel* level)
    bint price_level_is_empty(PriceLevel* level)

cdef class FastPriceLevel:
    """
    Cython wrapper around pure-C PriceLevel queue.
    Maintains FIFO Time-Priority for all orders resting at a specific price.
    """
    cdef PriceLevel* _level

    def __cinit__(self, int64_t price):
        self._level = <PriceLevel*>malloc(sizeof(PriceLevel))
        if self._level == NULL:
            raise MemoryError("Failed to allocate PriceLevel")
        price_level_init(self._level, price)

    def __dealloc__(self):
        if self._level != NULL:
            free(self._level)
            self._level = NULL

    @property
    def price(self) -> int:
        return self._level.price if self._level != NULL else 0

    @property
    def total_volume(self) -> int:
        return self._level.total_volume if self._level != NULL else 0

    @property
    def order_count(self) -> int:
        return self._level.order_count if self._level != NULL else 0

    @property
    def is_empty(self) -> bool:
        return price_level_is_empty(self._level) if self._level != NULL else True

    def append(self, size_t node_ptr) -> None:
        """Append an order node to the tail of the FIFO queue."""
        cdef OrderNode* node = <OrderNode*>node_ptr
        if node != NULL and self._level != NULL:
            with nogil:
                price_level_append(self._level, node)

    def remove(self, size_t node_ptr) -> None:
        """Unlink an order node from any position in the queue."""
        cdef OrderNode* node = <OrderNode*>node_ptr
        if node != NULL and self._level != NULL:
            with nogil:
                price_level_remove(self._level, node)

    def peek_head_id(self) -> int:
        """Return the order_id of the oldest resting order at this price (FIFO head)."""
        cdef OrderNode* head = price_level_peek(self._level)
        if head == NULL:
            return 0
        return head.order_id

    def peek_head_ptr(self) -> size_t:
        """Return raw pointer to head order node."""
        cdef OrderNode* head = price_level_peek(self._level)
        return <size_t>head

    def benchmark_append_remove(self, size_t node_ptr, size_t iterations) -> float:
        """
        Measure pure C-level FIFO append + remove time per cycle inside nogil.
        """
        cdef OrderNode* node = <OrderNode*>node_ptr
        cdef PriceLevel* level = self._level
        cdef size_t i
        cdef uint64_t t0, t1
        import time

        if node == NULL or level == NULL:
            return 0.0

        t0 = time.perf_counter_ns()
        with nogil:
            for i in range(iterations):
                price_level_append(level, node)
                price_level_remove(level, node)
        t1 = time.perf_counter_ns()

        cdef double elapsed_ns = <double>(t1 - t0)
        return elapsed_ns / (<double>iterations)
