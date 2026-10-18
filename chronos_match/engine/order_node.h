/**
 * ChronosMatch: Limit Order Book Engine
 * OrderNode: Intrusive Doubly-Linked Node Structure
 * 
 * Hardware cache-aligned to 64 bytes to guarantee single-cycle cache line fetch.
 */

#ifndef CHRONOS_ORDER_NODE_H
#define CHRONOS_ORDER_NODE_H

#include <stdint.h>
#include <stddef.h>

#ifdef _MSC_VER
  #pragma pack(push, 1)
#endif

typedef struct OrderNode OrderNode;

/**
 * OrderNode: Represents a resting limit order in the order book.
 * Intrusive pointers (prev, next) allow O(1) insertion into price queues
 * and O(1) removal during cancellations without memory allocations.
 */
struct OrderNode {
    /* Intrusive linked-list pointers (16 bytes on 64-bit) */
    OrderNode* prev;            /* Previous order at identical price level */
    OrderNode* next;            /* Next order at identical price level (or free-list link) */

    /* Order Financial Data */
    uint64_t   order_id;        /* Unique order ID (8 bytes) */
    uint64_t   client_id;       /* Client / Participant ID (8 bytes) */
    int64_t    price;           /* Fixed-point price (8 bytes, scaled by 10,000) */
    uint32_t   quantity;        /* Resting unfilled quantity (4 bytes) */
    uint16_t   symbol_id;       /* Instrument symbol ID (2 bytes) */
    uint8_t    side;            /* 0 = Buy, 1 = Sell (1 byte) */
    uint8_t    order_type;      /* 0 = Limit, 1 = Market, 2 = Cancel (1 byte) */
    uint64_t   timestamp_in_ns; /* Ingress timestamp in nanoseconds (8 bytes) */
    
    /* 8 bytes padding to align struct to exactly 64 bytes (1 CPU cache line) */
    uint8_t    pad[8];
};

#ifdef _MSC_VER
  #pragma pack(pop)
#endif

#endif /* CHRONOS_ORDER_NODE_H */
