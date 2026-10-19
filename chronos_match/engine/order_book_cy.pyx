# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True

cimport cython
from libc.stdint cimport uint64_t, int64_t, uint32_t, uint16_t, uint8_t
from libc.stdlib cimport malloc, free
from libc.stddef cimport size_t
import time

cdef extern from "arena.h" nogil:
    ctypedef struct OrderArena:
        pass
    OrderArena* arena_create(size_t capacity)
    void arena_destroy(OrderArena* arena)

cdef extern from "order_book.h" nogil:
    ctypedef struct MatchEvent:
        uint64_t trade_id
        uint64_t buy_order_id
        uint64_t sell_order_id
        int64_t  execution_price
        uint32_t quantity
        uint8_t  aggressor_side
        uint64_t match_timestamp_ns

    ctypedef struct OrderBook:
        uint64_t total_trades_matched
        uint64_t total_shares_traded

    void order_book_init(OrderBook* book, OrderArena* arena)
    void order_book_destroy(OrderBook* book)
    bint order_book_cancel_order(OrderBook* book, uint64_t order_id)
    size_t order_book_process_limit(
        OrderBook* book,
        uint64_t order_id,
        uint64_t client_id,
        int64_t price,
        uint32_t quantity,
        uint8_t side,
        uint16_t symbol_id,
        uint64_t timestamp_ns,
        MatchEvent* out_matches,
        size_t max_matches
    )
    int64_t order_book_best_bid_price(const OrderBook* book)
    uint64_t order_book_best_bid_volume(const OrderBook* book)
    int64_t order_book_best_ask_price(const OrderBook* book)
    uint64_t order_book_best_ask_volume(const OrderBook* book)
    int64_t order_book_spread(const OrderBook* book)

    ctypedef struct L2Level:
        int64_t  price
        uint64_t volume
        uint32_t order_count

    size_t order_book_get_l2_bids(const OrderBook* book, L2Level* out_levels, size_t max_depth)
    size_t order_book_get_l2_asks(const OrderBook* book, L2Level* out_levels, size_t max_depth)

cdef class FastOrderBook:
    """
    Cython wrapper around pure-C OrderBook matching engine.
    Executes Price-Time Priority matching inside nogil with zero malloc.
    """
    cdef OrderBook*  _book
    cdef OrderArena* _arena

    def __cinit__(self, size_t arena_capacity=100000):
        self._arena = arena_create(arena_capacity)
        if self._arena == NULL:
            raise MemoryError("Failed to allocate OrderArena for OrderBook")

        self._book = <OrderBook*>malloc(sizeof(OrderBook))
        if self._book == NULL:
            arena_destroy(self._arena)
            raise MemoryError("Failed to allocate OrderBook")

        order_book_init(self._book, self._arena)

    def __dealloc__(self):
        if self._book != NULL:
            order_book_destroy(self._book)
            free(self._book)
            self._book = NULL
        if self._arena != NULL:
            arena_destroy(self._arena)
            self._arena = NULL

    @property
    def best_bid_price(self) -> int:
        return order_book_best_bid_price(self._book) if self._book != NULL else 0

    @property
    def best_bid_volume(self) -> int:
        return order_book_best_bid_volume(self._book) if self._book != NULL else 0

    @property
    def best_ask_price(self) -> int:
        return order_book_best_ask_price(self._book) if self._book != NULL else 0

    @property
    def best_ask_volume(self) -> int:
        return order_book_best_ask_volume(self._book) if self._book != NULL else 0

    @property
    def spread(self) -> int:
        return order_book_spread(self._book) if self._book != NULL else 0

    @property
    def total_trades_matched(self) -> int:
        return self._book.total_trades_matched if self._book != NULL else 0

    @property
    def total_shares_traded(self) -> int:
        return self._book.total_shares_traded if self._book != NULL else 0

    def get_l2_snapshot(self, size_t depth=10) -> dict:
        """
        Extract top N bids and asks for Level 2 market depth visualization.
        Operates inside nogil with zero dynamic heap allocation.
        """
        cdef L2Level bid_buf[32]
        cdef L2Level ask_buf[32]
        cdef size_t max_d = depth if depth <= 32 else 32
        cdef size_t n_bids = 0
        cdef size_t n_asks = 0
        cdef OrderBook* book = self._book

        if book != NULL:
            with nogil:
                n_bids = order_book_get_l2_bids(book, bid_buf, max_d)
                n_asks = order_book_get_l2_asks(book, ask_buf, max_d)

        cdef list bids = []
        cdef list asks = []
        cdef size_t i
        for i in range(n_bids):
            bids.append({
                "price": bid_buf[i].price,
                "volume": bid_buf[i].volume,
                "order_count": bid_buf[i].order_count,
            })
        for i in range(n_asks):
            asks.append({
                "price": ask_buf[i].price,
                "volume": ask_buf[i].volume,
                "order_count": ask_buf[i].order_count,
            })

        return {"bids": bids, "asks": asks}

    def add_limit_order(
        self,
        uint64_t order_id,
        uint64_t client_id,
        int64_t price,
        uint32_t quantity,
        uint8_t side,
        uint16_t symbol_id=1,
        uint64_t timestamp_ns=0
    ) -> list:
        """
        Process incoming Limit Order and return generated match events.
        """
        cdef MatchEvent matches[32] # Stack-allocated buffer for matches
        cdef size_t match_count
        cdef OrderBook* book = self._book

        if timestamp_ns == 0:
            timestamp_ns = time.perf_counter_ns()

        with nogil:
            match_count = order_book_process_limit(
                book,
                order_id,
                client_id,
                price,
                quantity,
                side,
                symbol_id,
                timestamp_ns,
                matches,
                32
            )

        cdef list results = []
        cdef size_t i
        for i in range(match_count):
            results.append({
                "trade_id": matches[i].trade_id,
                "buy_order_id": matches[i].buy_order_id,
                "sell_order_id": matches[i].sell_order_id,
                "execution_price": matches[i].execution_price,
                "quantity": matches[i].quantity,
                "aggressor_side": matches[i].aggressor_side,
                "match_timestamp_ns": matches[i].match_timestamp_ns,
            })

        return results

    def benchmark_matching(self, size_t pairs) -> float:
        """
        Benchmark raw C-level matching throughput inside nogil.
        Alternates between placing resting Ask and aggressive Buy.
        Returns average execution time in nanoseconds per order.
        """
        cdef size_t i
        cdef uint64_t t0, t1
        cdef OrderBook* book = self._book
        cdef MatchEvent match_buf[4]
        cdef int64_t price = 1500000 # $150.00

        t0 = time.perf_counter_ns()
        with nogil:
            for i in range(pairs):
                # 1. Place resting Ask at $150.00
                order_book_process_limit(book, (i * 2) + 1, 1, price, 100, 1, 1, 0, match_buf, 4)
                # 2. Place aggressive Buy at $150.00 (triggers instant match)
                order_book_process_limit(book, (i * 2) + 2, 2, price, 100, 0, 1, 0, match_buf, 4)
        t1 = time.perf_counter_ns()

        cdef double elapsed_ns = <double>(t1 - t0)
        return elapsed_ns / (<double>(pairs * 2)) # ns per order

    def cancel_order(self, uint64_t order_id) -> bool:
        """
        Instantaneous O(1) order cancellation by order_id.
        Returns True if cancelled, False if not found or already filled.
        """
        cdef bint result
        cdef OrderBook* book = self._book
        with nogil:
            result = order_book_cancel_order(book, order_id)
        return bool(result)

    def benchmark_cancellations(self, size_t count) -> float:
        """
        Benchmark raw C-level order placement and O(1) cancellation in nogil.
        Returns average nanoseconds per cancellation.
        """
        cdef size_t i
        cdef uint64_t t0, t1
        cdef OrderBook* book = self._book
        cdef MatchEvent match_buf[2]
        cdef int64_t base_price = 1000000 # $100.00

        # Pre-place 'count' orders across 50 active price levels so they rest cleanly
        for i in range(count):
            order_book_process_limit(book, i + 1, 1, base_price + (<int64_t>(i % 50) * 100), 100, 1, 1, 0, match_buf, 2)

        t0 = time.perf_counter_ns()
        with nogil:
            for i in range(count):
                order_book_cancel_order(book, i + 1)
        t1 = time.perf_counter_ns()

        cdef double elapsed_ns = <double>(t1 - t0)
        return elapsed_ns / (<double>count)
