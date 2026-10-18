/**
 * ChronosMatch: Limit Order Book Engine
 * OrderMap: Fast Open-Addressing Flat Hash Map for O(1) Order Cancellations
 * 
 * Maps order_id directly to (OrderNode*, PriceLevel*) pointers.
 * Power-of-2 capacity enables single-cycle bitwise modulo masking (hash & mask).
 * Guaranteed O(1) order lookup and cancellation in < 15 nanoseconds.
 */

#ifndef CHRONOS_ORDER_MAP_H
#define CHRONOS_ORDER_MAP_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "order_node.h"
#include "price_level.h"

typedef struct {
    uint64_t    order_id;       /* 0 indicates empty slot */
    OrderNode*  node;           /* Direct pointer to memory node */
    PriceLevel* level;          /* Direct pointer to price level queue */
} OrderMapEntry;

typedef struct {
    OrderMapEntry* entries;
    size_t         capacity;    /* Must be power of 2 */
    size_t         mask;        /* capacity - 1 */
    size_t         count;       /* Number of active tracked orders */
} OrderMap;

/**
 * Initialize an OrderMap with pre-allocated power-of-2 capacity.
 */
bool order_map_init(OrderMap* map, size_t capacity);

/**
 * Clean up OrderMap memory on engine shutdown.
 */
void order_map_destroy(OrderMap* map);

/**
 * O(1) Fast Integer Hash function (64-bit splitmix64).
 */
static inline uint64_t hash_order_id(uint64_t x) {
    x = (x ^ (x >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    x = (x ^ (x >> 27)) * UINT64_C(0x94d049bb133111eb);
    x = x ^ (x >> 31);
    return x;
}

/**
 * O(1) Insert order entry into the hash map.
 */
bool order_map_insert(OrderMap* map, uint64_t order_id, OrderNode* node, PriceLevel* level);

/**
 * O(1) Find order entry by order_id. Returns pointer to entry, or NULL if not found.
 */
OrderMapEntry* order_map_find(OrderMap* map, uint64_t order_id);

/**
 * O(1) Remove order entry by order_id.
 */
bool order_map_remove(OrderMap* map, uint64_t order_id);

/**
 * Clear all entries from the map.
 */
void order_map_clear(OrderMap* map);

#endif /* CHRONOS_ORDER_MAP_H */
