/**
 * ChronosMatch: Zero-Copy High-Frequency Trading Engine
 * Core C-Struct Types and Memory Alignment Definitions
 * 
 * Target: Sub-50 microsecond execution, Zero-Copy shared memory IPC
 */

#ifndef CHRONOS_TYPES_H
#define CHRONOS_TYPES_H

#include <stdint.h>
#include <stdbool.h>

#ifdef _MSC_VER
  #define ALIGN_64 __declspec(align(64))
  #pragma pack(push, 1)
#else
  #define ALIGN_64 __attribute__((aligned(64)))
  #define PACKED __attribute__((packed))
#endif

/* Fixed-point arithmetic scale: 4 decimals (e.g. $100.25 -> 1,002,500) */
#define PRICE_SCALE 10000LL

typedef enum {
    SIDE_BUY = 0,
    SIDE_SELL = 1
} OrderSide;

typedef enum {
    TYPE_LIMIT = 0,
    TYPE_MARKET = 1,
    TYPE_CANCEL = 2
} OrderType;

/**
 * OrderPacket: 64-byte cache-line aligned order message.
 * Stored directly in the SPSC shared memory ring buffer.
 * Fitting in exactly one 64-byte cache line guarantees single-transaction CPU memory fetches.
 */
typedef struct {
    uint64_t order_id;          /* Unique order identifier (8 bytes) */
    uint64_t client_id;         /* Trader / Client identifier (8 bytes) */
    int64_t  price;             /* Fixed-point price (8 bytes, $123.45 -> 1234500) */
    uint32_t quantity;          /* Order size / shares (4 bytes) */
    uint8_t  side;              /* 0 = Buy, 1 = Sell (1 byte) */
    uint8_t  order_type;        /* 0 = Limit, 1 = Market, 2 = Cancel (1 byte) */
    uint16_t symbol_id;         /* Symbol code e.g. 1 = AAPL, 2 = TSLA (2 bytes) */
    uint64_t timestamp_in_ns;   /* Ingress timestamp from perf_counter_ns (8 bytes) */
    uint8_t  padding[24];       /* Padding to align to exactly 64 bytes */
} OrderPacket;

/**
 * TradePacket: 64-byte cache-line aligned trade execution message.
 * Emitted by the matching engine into the trade output ring buffer.
 */
typedef struct {
    uint64_t trade_id;          /* Monotonically increasing execution ID (8 bytes) */
    uint64_t buy_order_id;      /* Matching Buy Order ID (8 bytes) */
    uint64_t sell_order_id;     /* Matching Sell Order ID (8 bytes) */
    int64_t  price;             /* Execution price (8 bytes) */
    uint32_t quantity;          /* Matched fill quantity (4 bytes) */
    uint16_t symbol_id;         /* Symbol ID (2 bytes) */
    uint8_t  aggressor_side;    /* Side that initiated the cross (1 byte) */
    uint8_t  reserved;          /* Alignment byte (1 byte) */
    uint64_t match_timestamp_ns;/* Timestamp when match occurred (8 bytes) */
    uint64_t latency_ns;        /* End-to-end latency: match_ns - order_ingress_ns (8 bytes) */
    uint8_t  padding[8];        /* Padding to align to exactly 64 bytes */
} TradePacket;

/**
 * RingBufferHeader: Layout for Single-Producer Single-Consumer (SPSC) shared memory.
 * CRITICAL HFT OPTIMIZATION:
 * 'head' (Producer write cursor) and 'tail' (Consumer read cursor) are placed on 
 * SEPARATE 64-byte cache lines to eliminate CPU cache false sharing.
 */
typedef struct {
    /* Cache Line 1: Producer Write Cursor */
    volatile uint64_t head;
    uint8_t  pad1[56];          /* Ensures tail is on the next cache line */

    /* Cache Line 2: Consumer Read Cursor */
    volatile uint64_t tail;
    uint8_t  pad2[56];          /* Ensures metadata is on the next cache line */

    /* Cache Line 3: Configuration & Ring Metadata */
    uint32_t capacity;          /* Must be power of 2 (e.g. 65536, 1048576) */
    uint32_t mask;              /* capacity - 1, for bitwise fast modulo (pos & mask) */
    uint32_t slot_size;         /* Size of each slot in bytes (e.g. 64) */
    uint32_t is_active;         /* 1 = Active, 0 = Shutdown signal */
    uint8_t  pad3[48];
} RingBufferHeader;

#ifdef _MSC_VER
  #pragma pack(pop)
#endif

#endif /* CHRONOS_TYPES_H */
