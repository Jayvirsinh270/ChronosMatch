/**
 * ChronosMatch: Limit Order Book Engine
 * PriceLevel: Intrusive Doubly-Linked FIFO Queue at a Single Price Tick
 * 
 * Implements Time Priority (FIFO):
 * - New resting orders append to tail in O(1) time.
 * - Aggressive matching orders consume liquidity from head in O(1) time.
 * - Cancellations unlink from arbitrary positions in O(1) time without list traversal.
 */

#ifndef CHRONOS_PRICE_LEVEL_H
#define CHRONOS_PRICE_LEVEL_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "order_node.h"

typedef struct PriceLevel PriceLevel;

struct PriceLevel {
    int64_t     price;          /* Fixed-point price (scaled by 10,000) */
    uint64_t    total_volume;   /* Total shares resting across all orders at this level */
    uint32_t    order_count;    /* Number of active resting orders */

    OrderNode*  head;           /* Oldest order (First to be matched) */
    OrderNode*  tail;           /* Newest order (Last to be matched) */

    /* Pointers to adjacent price levels in the book ladder */
    PriceLevel* next_level;
    PriceLevel* prev_level;
};

/**
 * Initialize a PriceLevel with a specific fixed-point price.
 */
static inline void price_level_init(PriceLevel* level, int64_t price) {
    level->price = price;
    level->total_volume = 0;
    level->order_count = 0;
    level->head = NULL;
    level->tail = NULL;
    level->next_level = NULL;
    level->prev_level = NULL;
}

/**
 * O(1) Append at Tail: Adds a newly arriving resting limit order.
 * Maintains Time Priority (FIFO).
 */
static inline void price_level_append(PriceLevel* level, OrderNode* node) {
    node->next = NULL;
    node->prev = level->tail;

    if (level->tail != NULL) {
        level->tail->next = node;
    } else {
        /* List was empty, node becomes head */
        level->head = node;
    }

    level->tail = node;
    level->total_volume += node->quantity;
    level->order_count++;
}

/**
 * O(1) Arbitrary Removal: Unlinks a node from the price level.
 * Used when an order is cancelled or filled completely.
 */
static inline void price_level_remove(PriceLevel* level, OrderNode* node) {
    if (node->prev != NULL) {
        node->prev->next = node->next;
    } else {
        /* Node was head */
        level->head = node->next;
    }

    if (node->next != NULL) {
        node->next->prev = node->prev;
    } else {
        /* Node was tail */
        level->tail = node->prev;
    }

    level->total_volume -= node->quantity;
    level->order_count--;

    node->prev = NULL;
    node->next = NULL;
}

/**
 * O(1) Peek Head: Inspect the oldest order without removing it.
 */
static inline OrderNode* price_level_peek(PriceLevel* level) {
    return level->head;
}

/**
 * Check if the price level has no remaining active orders.
 */
static inline bool price_level_is_empty(PriceLevel* level) {
    return level->head == NULL || level->order_count == 0;
}

#endif /* CHRONOS_PRICE_LEVEL_H */
