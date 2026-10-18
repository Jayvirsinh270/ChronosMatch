/**
 * ChronosMatch: Limit Order Book Engine
 * OrderBook Core Implementation (Price-Time Priority)
 */

#include "order_book.h"
#include <string.h>

void order_book_init(OrderBook* book, OrderArena* arena) {
    memset(book, 0, sizeof(OrderBook));
    book->arena = arena;
    book->next_trade_id = 1;
    book->level_pool_used = 0;
    order_map_init(&book->order_map, 65536);
}

void order_book_destroy(OrderBook* book) {
    if (book != NULL) {
        order_map_destroy(&book->order_map);
    }
}

/* --- Internal Price Level Pool & Ladder Management --- */

static PriceLevel* allocate_level(OrderBook* book, int64_t price) {
    if (book->level_pool_used >= MAX_PRICE_LEVELS) {
        return NULL;
    }
    PriceLevel* level = &book->level_pool[book->level_pool_used++];
    price_level_init(level, price);
    return level;
}

static PriceLevel* get_or_create_bid_level(OrderBook* book, int64_t price) {
    PriceLevel* curr = book->best_bid;
    PriceLevel* prev = NULL;

    /* Search existing ladder (descending order) */
    while (curr != NULL) {
        if (curr->price == price) {
            return curr;
        }
        if (curr->price < price) {
            break; /* Insertion point is before curr */
        }
        prev = curr;
        curr = curr->next_level;
    }

    /* Not found: allocate and insert */
    PriceLevel* new_level = allocate_level(book, price);
    if (!new_level) return NULL;

    if (prev == NULL) {
        /* New Highest Bid (Top of Book) */
        new_level->next_level = book->best_bid;
        if (book->best_bid != NULL) {
            book->best_bid->prev_level = new_level;
        }
        book->best_bid = new_level;
    } else {
        /* Insert between prev and curr */
        new_level->next_level = curr;
        new_level->prev_level = prev;
        prev->next_level = new_level;
        if (curr != NULL) {
            curr->prev_level = new_level;
        }
    }

    return new_level;
}

static PriceLevel* get_or_create_ask_level(OrderBook* book, int64_t price) {
    PriceLevel* curr = book->best_ask;
    PriceLevel* prev = NULL;

    /* Search existing ladder (ascending order) */
    while (curr != NULL) {
        if (curr->price == price) {
            return curr;
        }
        if (curr->price > price) {
            break; /* Insertion point is before curr */
        }
        prev = curr;
        curr = curr->next_level;
    }

    /* Not found: allocate and insert */
    PriceLevel* new_level = allocate_level(book, price);
    if (!new_level) return NULL;

    if (prev == NULL) {
        /* New Lowest Ask (Top of Book) */
        new_level->next_level = book->best_ask;
        if (book->best_ask != NULL) {
            book->best_ask->prev_level = new_level;
        }
        book->best_ask = new_level;
    } else {
        /* Insert between prev and curr */
        new_level->next_level = curr;
        new_level->prev_level = prev;
        prev->next_level = new_level;
        if (curr != NULL) {
            curr->prev_level = new_level;
        }
    }

    return new_level;
}

static void unlink_bid_level(OrderBook* book, PriceLevel* level) {
    if (level->prev_level != NULL) {
        level->prev_level->next_level = level->next_level;
    } else {
        book->best_bid = level->next_level;
    }

    if (level->next_level != NULL) {
        level->next_level->prev_level = level->prev_level;
    }

    level->next_level = NULL;
    level->prev_level = NULL;
}

static void unlink_ask_level(OrderBook* book, PriceLevel* level) {
    if (level->prev_level != NULL) {
        level->prev_level->next_level = level->next_level;
    } else {
        book->best_ask = level->next_level;
    }

    if (level->next_level != NULL) {
        level->next_level->prev_level = level->prev_level;
    }

    level->next_level = NULL;
    level->prev_level = NULL;
}

/* --- Core Price-Time Priority Matching Engine --- */

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
) {
    size_t match_count = 0;
    uint32_t remaining_qty = quantity;

    if (side == 0) {
        /* === BUY ORDER === */
        /* Match against Asks while Buy Price >= Best Ask Price */
        while (book->best_ask != NULL && price >= book->best_ask->price && remaining_qty > 0) {
            PriceLevel* ask_level = book->best_ask;

            while (ask_level->head != NULL && remaining_qty > 0) {
                OrderNode* passive_order = ask_level->head;
                uint32_t fill_qty = (remaining_qty < passive_order->quantity) ? remaining_qty : passive_order->quantity;

                if (out_matches != NULL && match_count < max_matches) {
                    MatchEvent* match = &out_matches[match_count++];
                    match->trade_id = book->next_trade_id++;
                    match->buy_order_id = order_id;
                    match->sell_order_id = passive_order->order_id;
                    match->execution_price = passive_order->price; /* Passive price priority */
                    match->quantity = fill_qty;
                    match->aggressor_side = 0; /* BUY aggressed */
                    match->match_timestamp_ns = timestamp_ns;
                }

                remaining_qty -= fill_qty;
                passive_order->quantity -= fill_qty;
                ask_level->total_volume -= fill_qty;
                book->total_shares_traded += fill_qty;
                book->total_trades_matched++;

                if (passive_order->quantity == 0) {
                    order_map_remove(&book->order_map, passive_order->order_id);
                    price_level_remove(ask_level, passive_order);
                    arena_free(book->arena, passive_order);
                }
            }

            if (price_level_is_empty(ask_level)) {
                unlink_ask_level(book, ask_level);
            }
        }

        /* Rest unfilled remainder in Bid ladder */
        if (remaining_qty > 0) {
            OrderNode* node = arena_alloc(book->arena);
            if (node != NULL) {
                node->order_id = order_id;
                node->client_id = client_id;
                node->price = price;
                node->quantity = remaining_qty;
                node->side = 0;
                node->symbol_id = symbol_id;
                node->timestamp_in_ns = timestamp_ns;

                PriceLevel* bid_level = get_or_create_bid_level(book, price);
                if (bid_level != NULL) {
                    price_level_append(bid_level, node);
                    order_map_insert(&book->order_map, order_id, node, bid_level);
                }
            }
        }
    } else {
        /* === SELL ORDER === */
        /* Match against Bids while Sell Price <= Best Bid Price */
        while (book->best_bid != NULL && price <= book->best_bid->price && remaining_qty > 0) {
            PriceLevel* bid_level = book->best_bid;

            while (bid_level->head != NULL && remaining_qty > 0) {
                OrderNode* passive_order = bid_level->head;
                uint32_t fill_qty = (remaining_qty < passive_order->quantity) ? remaining_qty : passive_order->quantity;

                if (out_matches != NULL && match_count < max_matches) {
                    MatchEvent* match = &out_matches[match_count++];
                    match->trade_id = book->next_trade_id++;
                    match->buy_order_id = passive_order->order_id;
                    match->sell_order_id = order_id;
                    match->execution_price = passive_order->price; /* Passive price priority */
                    match->quantity = fill_qty;
                    match->aggressor_side = 1; /* SELL aggressed */
                    match->match_timestamp_ns = timestamp_ns;
                }

                remaining_qty -= fill_qty;
                passive_order->quantity -= fill_qty;
                bid_level->total_volume -= fill_qty;
                book->total_shares_traded += fill_qty;
                book->total_trades_matched++;

                if (passive_order->quantity == 0) {
                    order_map_remove(&book->order_map, passive_order->order_id);
                    price_level_remove(bid_level, passive_order);
                    arena_free(book->arena, passive_order);
                }
            }

            if (price_level_is_empty(bid_level)) {
                unlink_bid_level(book, bid_level);
            }
        }

        /* Rest unfilled remainder in Sell ladder */
        if (remaining_qty > 0) {
            OrderNode* node = arena_alloc(book->arena);
            if (node != NULL) {
                node->order_id = order_id;
                node->client_id = client_id;
                node->price = price;
                node->quantity = remaining_qty;
                node->side = 1;
                node->symbol_id = symbol_id;
                node->timestamp_in_ns = timestamp_ns;

                PriceLevel* ask_level = get_or_create_ask_level(book, price);
                if (ask_level != NULL) {
                    price_level_append(ask_level, node);
                    order_map_insert(&book->order_map, order_id, node, ask_level);
                }
            }
        }
    }

    return match_count;
}

bool order_book_cancel_order(OrderBook* book, uint64_t order_id) {
    OrderMapEntry* entry = order_map_find(&book->order_map, order_id);
    if (entry == NULL) {
        return false;
    }

    OrderNode* node = entry->node;
    PriceLevel* level = entry->level;

    price_level_remove(level, node);

    if (price_level_is_empty(level)) {
        if (node->side == 0) {
            unlink_bid_level(book, level);
        } else {
            unlink_ask_level(book, level);
        }
    }

    arena_free(book->arena, node);
    order_map_remove(&book->order_map, order_id);
    return true;
}
