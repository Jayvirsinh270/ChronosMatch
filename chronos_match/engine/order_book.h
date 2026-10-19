/**
 * ChronosMatch: Limit Order Book Engine
 * OrderBook: Price-Time Priority Matching Engine Core
 * 
 * Features:
 * - Integer fixed-point pricing (int64_t scaled by 10,000, zero float inaccuracies).
 * - Pre-allocated memory arena for OrderNodes (zero malloc in the hot path).
 * - Price ladders for Bids (descending) and Asks (ascending).
 * - High-speed trade execution generation.
 */

#ifndef CHRONOS_ORDER_BOOK_H
#define CHRONOS_ORDER_BOOK_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "order_node.h"
#include "price_level.h"
#include "arena.h"
#include "order_map.h"

#define MAX_PRICE_LEVELS 1024

typedef struct {
    uint64_t trade_id;
    uint64_t buy_order_id;
    uint64_t sell_order_id;
    int64_t  execution_price;
    uint32_t quantity;
    uint8_t  aggressor_side;    /* 0 = Buy crossed spread, 1 = Sell crossed spread */
    uint64_t match_timestamp_ns;
} MatchEvent;

typedef struct {
    int64_t  price;
    uint64_t volume;
    uint32_t order_count;
} L2Level;

typedef struct {
    OrderArena* arena;
    OrderMap    order_map;      /* O(1) order_id -> (OrderNode*, PriceLevel*) lookup */

    /* Top-of-Book pointers */
    PriceLevel* best_bid;       /* Highest active bid price level */
    PriceLevel* best_ask;       /* Lowest active ask price level */

    /* Pre-allocated pool of PriceLevels to avoid malloc during trading */
    PriceLevel  level_pool[MAX_PRICE_LEVELS];
    size_t      level_pool_used;

    /* Trade execution metrics */
    uint64_t    next_trade_id;
    uint64_t    total_trades_matched;
    uint64_t    total_shares_traded;
} OrderBook;

/**
 * Initialize an OrderBook instance backed by a pre-allocated OrderArena.
 */
void order_book_init(OrderBook* book, OrderArena* arena);

/**
 * Clean up order book resources on shutdown.
 */
void order_book_destroy(OrderBook* book);

/**
 * Instantaneous O(1) Order Cancellation by order_id.
 * Returns true if successfully unlinked and recycled, false if not found.
 */
bool order_book_cancel_order(OrderBook* book, uint64_t order_id);

/**
 * Process an incoming Limit Order:
 * 1. Checks opposite book side for crosses (Buy.price >= BestAsk or Sell.price <= BestBid).
 * 2. Matches quantities under Price-Time Priority.
 * 3. Rests any unfilled remainder in the appropriate price level.
 * 
 * Returns number of trade executions generated.
 */
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
);

/**
 * Top of Book Inspection
 */
static inline int64_t order_book_best_bid_price(const OrderBook* book) {
    return book->best_bid != NULL ? book->best_bid->price : 0;
}

static inline uint64_t order_book_best_bid_volume(const OrderBook* book) {
    return book->best_bid != NULL ? book->best_bid->total_volume : 0;
}

static inline int64_t order_book_best_ask_price(const OrderBook* book) {
    return book->best_ask != NULL ? book->best_ask->price : 0;
}

static inline uint64_t order_book_best_ask_volume(const OrderBook* book) {
    return book->best_ask != NULL ? book->best_ask->total_volume : 0;
}

static inline int64_t order_book_spread(const OrderBook* book) {
    if (book->best_ask == NULL || book->best_bid == NULL) return 0;
    return book->best_ask->price - book->best_bid->price;
}

/**
 * Level 2 Market Depth Extraction (Zero Malloc)
 * Copies top N active price levels into caller-supplied out_levels buffer.
 * Returns the number of levels populated.
 */
size_t order_book_get_l2_bids(const OrderBook* book, L2Level* out_levels, size_t max_depth);
size_t order_book_get_l2_asks(const OrderBook* book, L2Level* out_levels, size_t max_depth);

#endif /* CHRONOS_ORDER_BOOK_H */
