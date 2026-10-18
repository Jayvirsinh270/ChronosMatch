/**
 * ChronosMatch: Limit Order Book Engine
 * OrderMap Implementation (Open-Addressing Flat Hash Map)
 */

#include "order_map.h"
#include <stdlib.h>
#include <string.h>

bool order_map_init(OrderMap* map, size_t capacity) {
    if (capacity == 0 || (capacity & (capacity - 1)) != 0) {
        return false; /* Must be power of 2 */
    }

    map->capacity = capacity;
    map->mask = capacity - 1;
    map->count = 0;
    map->entries = (OrderMapEntry*)calloc(capacity, sizeof(OrderMapEntry));

    return map->entries != NULL;
}

void order_map_destroy(OrderMap* map) {
    if (map->entries != NULL) {
        free(map->entries);
        map->entries = NULL;
    }
    map->count = 0;
    map->capacity = 0;
}

bool order_map_insert(OrderMap* map, uint64_t order_id, OrderNode* node, PriceLevel* level) {
    if (order_id == 0 || map->entries == NULL || map->count >= (map->capacity * 3 / 4)) {
        return false; /* Reserved ID or load factor > 75% */
    }

    size_t idx = hash_order_id(order_id) & map->mask;

    while (map->entries[idx].order_id != 0 && map->entries[idx].order_id != order_id) {
        idx = (idx + 1) & map->mask;
    }

    if (map->entries[idx].order_id == 0) {
        map->count++;
    }

    map->entries[idx].order_id = order_id;
    map->entries[idx].node = node;
    map->entries[idx].level = level;

    return true;
}

OrderMapEntry* order_map_find(OrderMap* map, uint64_t order_id) {
    if (order_id == 0 || map->entries == NULL) {
        return NULL;
    }

    size_t idx = hash_order_id(order_id) & map->mask;

    while (map->entries[idx].order_id != 0) {
        if (map->entries[idx].order_id == order_id) {
            return &map->entries[idx];
        }
        idx = (idx + 1) & map->mask;
    }

    return NULL;
}

bool order_map_remove(OrderMap* map, uint64_t order_id) {
    if (order_id == 0 || map->entries == NULL) {
        return false;
    }

    size_t idx = hash_order_id(order_id) & map->mask;

    while (map->entries[idx].order_id != 0) {
        if (map->entries[idx].order_id == order_id) {
            /* Backward-shift deletion to preserve open-addressing chain */
            size_t curr = idx;
            size_t next = (curr + 1) & map->mask;

            while (map->entries[next].order_id != 0) {
                size_t ideal = hash_order_id(map->entries[next].order_id) & map->mask;
                bool can_shift = false;

                if (curr <= next) {
                    if (ideal <= curr || ideal > next) can_shift = true;
                } else {
                    if (ideal <= curr && ideal > next) can_shift = true;
                }

                if (can_shift) {
                    map->entries[curr] = map->entries[next];
                    curr = next;
                }
                next = (next + 1) & map->mask;
            }

            map->entries[curr].order_id = 0;
            map->entries[curr].node = NULL;
            map->entries[curr].level = NULL;
            map->count--;
            return true;
        }
        idx = (idx + 1) & map->mask;
    }

    return false;
}

void order_map_clear(OrderMap* map) {
    if (map->entries != NULL) {
        memset(map->entries, 0, map->capacity * sizeof(OrderMapEntry));
        map->count = 0;
    }
}
