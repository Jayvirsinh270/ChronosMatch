/**
 * ChronosMatch: Limit Order Book Engine
 * OrderArena Implementation
 */

#include "arena.h"
#include <stdlib.h>
#include <string.h>

OrderArena* arena_create(size_t capacity) {
    if (capacity == 0) return NULL;

    OrderArena* arena = (OrderArena*)malloc(sizeof(OrderArena));
    if (!arena) return NULL;

    arena->capacity = capacity;
    arena->allocated_count = 0;
    arena->peak_count = 0;

    /* Allocate contiguous node array */
    arena->pool = (OrderNode*)calloc(capacity, sizeof(OrderNode));
    if (!arena->pool) {
        free(arena);
        return NULL;
    }

    /* Initialize intrusive free-list linking each node to its neighbor */
    for (size_t i = 0; i < capacity - 1; ++i) {
        arena->pool[i].next = &arena->pool[i + 1];
    }
    arena->pool[capacity - 1].next = NULL;
    arena->free_head = &arena->pool[0];

    return arena;
}

void arena_destroy(OrderArena* arena) {
    if (!arena) return;
    if (arena->pool) {
        free(arena->pool);
        arena->pool = NULL;
    }
    free(arena);
}

void arena_reset(OrderArena* arena) {
    if (!arena || !arena->pool) return;

    memset(arena->pool, 0, arena->capacity * sizeof(OrderNode));

    for (size_t i = 0; i < arena->capacity - 1; ++i) {
        arena->pool[i].next = &arena->pool[i + 1];
    }
    arena->pool[arena->capacity - 1].next = NULL;
    arena->free_head = &arena->pool[0];
    arena->allocated_count = 0;
}
