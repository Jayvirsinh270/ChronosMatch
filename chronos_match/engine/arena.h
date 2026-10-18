/**
 * ChronosMatch: Limit Order Book Engine
 * OrderArena: Pre-allocated Memory Arena & Intrusive Free-List Allocator
 * 
 * Guarantees zero dynamic memory allocation (malloc/free) in the matching hot path.
 * Allocation and deallocation execute in O(1) time (< 5 nanoseconds).
 */

#ifndef CHRONOS_ARENA_H
#define CHRONOS_ARENA_H

#include <stddef.h>
#include <stdbool.h>
#include "order_node.h"

typedef struct {
    OrderNode* pool;             /* Contiguous block of pre-allocated nodes */
    OrderNode* free_head;        /* Head of intrusive free-list */
    size_t     capacity;         /* Maximum nodes supported */
    size_t     allocated_count;  /* Current active nodes in use */
    size_t     peak_count;       /* High-water mark for memory profiling */
} OrderArena;

/**
 * Initialize a new OrderArena with pre-allocated node capacity.
 */
OrderArena* arena_create(size_t capacity);

/**
 * Free the entire arena memory block at engine shutdown.
 */
void arena_destroy(OrderArena* arena);

/**
 * O(1) Node Allocation: Pops the head of the intrusive free-list.
 * Returns pointer to OrderNode, or NULL if arena is exhausted.
 */
static inline OrderNode* arena_alloc(OrderArena* arena) {
    if (arena->free_head == NULL) {
        return NULL; /* Out of memory in pre-allocated pool */
    }
    OrderNode* node = arena->free_head;
    arena->free_head = node->next;
    
    node->prev = NULL;
    node->next = NULL;
    arena->allocated_count++;
    if (arena->allocated_count > arena->peak_count) {
        arena->peak_count = arena->allocated_count;
    }
    return node;
}

/**
 * O(1) Node Deallocation: Pushes the node back to the head of the intrusive free-list.
 * Executes in ~2 CPU cycles with zero OS intervention.
 */
static inline void arena_free(OrderArena* arena, OrderNode* node) {
    if (node == NULL) return;
    
    node->next = arena->free_head;
    arena->free_head = node;
    node->prev = NULL;
    arena->allocated_count--;
}

/**
 * Reset all nodes back to free list without deallocating the underlying memory block.
 */
void arena_reset(OrderArena* arena);

#endif /* CHRONOS_ARENA_H */
