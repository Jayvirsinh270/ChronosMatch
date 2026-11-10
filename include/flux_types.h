/**
 * FluxCore: Zero-Copy High-Frequency Trading Engine
 * Core C-Struct Types and Memory Alignment Definitions
 * 
 * Target: Sub-50 microsecond execution, Zero-Copy shared memory IPC
 */

#ifndef FLUX_TYPES_H
#define FLUX_TYPES_H

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
} ALIGN_64 OrderPacket;

/**
 * TradePacket: 64-byte cache-line aligned matched trade event.
 * Published to outbound SPSC ring buffer for asynchronous SQLite WAL logging.
 */
typedef struct {
    uint64_t trade_id;          /* Monotonically increasing execution ID */
    uint64_t buy_order_id;      /* Aggressor or resting Buy order ID */
    uint64_t sell_order_id;     /* Aggressor or resting Sell order ID */
    int64_t  price;             /* Match execution price (fixed-point) */
    uint32_t quantity;          /* Shares matched in this fill */
    uint16_t symbol_id;         /* Symbol identifier */
    uint8_t  aggressor_side;    /* 0 = Buy crossed the spread, 1 = Sell crossed */
    uint8_t  padding_inner;     /* Alignment pad byte */
    uint64_t match_timestamp_ns;/* Timestamp when match was finalized in nogil C loop */
    uint8_t  padding[16];       /* Fill to exactly 64 bytes */
} ALIGN_64 TradePacket;

/**
 * RingBufferHeader: 256-byte header mapped at byte offset 0 of the mmap file.
 * Cursors are spaced into separate 64-byte cache lines to eliminate CPU false sharing.
 */
typedef struct {
    /* Cache Line 0: Producer Write Cursor */
    ALIGN_64 volatile uint64_t head;
    uint8_t  pad0[56];

    /* Cache Line 1: Consumer Read Cursor */
    ALIGN_64 volatile uint64_t tail;
    uint8_t  pad1[56];

    /* Cache Line 2: Ring Buffer Dimensions & Control Flags */
    uint32_t capacity;          /* Slot count (must be power of 2) */
    uint32_t mask;              /* Bitmask: capacity - 1 */
    uint32_t slot_size;         /* Size of each slot in bytes (typically 64) */
    volatile uint32_t is_active;/* 1 = Running, 0 = Stopped/Shutdown */
    uint8_t  pad2[48];

    /* Cache Line 3: Reserved for Future Multi-Symbol Extensions */
    uint8_t  reserved[64];
} ALIGN_64 RingBufferHeader;

#ifdef _MSC_VER
  #pragma pack(pop)
#endif

#endif /* FLUX_TYPES_H */
