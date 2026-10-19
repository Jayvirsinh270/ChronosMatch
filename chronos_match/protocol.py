"""
ChronosMatch Protocol & Binary Serialization Layer.
Aligns 1:1 with C structs defined in chronos_types.h.
"""

from enum import IntEnum
import struct
from typing import NamedTuple, Tuple

# Fixed-point price scaling (4 decimal places: $100.25 -> 1,002,500)
PRICE_SCALE = 10_000

class OrderSide(IntEnum):
    BUY = 0
    SELL = 1

Side = OrderSide

class OrderType(IntEnum):
    LIMIT = 0
    MARKET = 1
    CANCEL = 2

def to_fixed_price(price: float) -> int:
    """Convert float price to int64 fixed-point integer (avoids float inaccuracies)."""
    return int(round(price * PRICE_SCALE))

def from_fixed_price(fixed_price: int) -> float:
    """Convert int64 fixed-point integer back to float."""
    return fixed_price / PRICE_SCALE

# Format strings for 64-byte structs
# OrderPacket: Q=order_id, Q=client_id, q=price, I=quantity, B=side, B=type, H=symbol, Q=timestamp, 24s=padding
ORDER_FORMAT = "=QQqIBBHQ24s"
ORDER_SIZE = struct.calcsize(ORDER_FORMAT) # 64 bytes
assert ORDER_SIZE == 64, f"Order struct must be 64 bytes, got {ORDER_SIZE}"

# TradePacket: Q=trade_id, Q=buy_id, Q=sell_id, q=price, I=quantity, H=symbol, B=aggressor, B=reserved, Q=timestamp, Q=latency, 8s=padding
TRADE_FORMAT = "=QQQqIHBBQQ8s"
TRADE_SIZE = struct.calcsize(TRADE_FORMAT) # 64 bytes
assert TRADE_SIZE == 64, f"Trade struct must be 64 bytes, got {TRADE_SIZE}"

# RingBufferHeader: 192 bytes (3 cache lines of 64 bytes)
HEADER_FORMAT = "=Q56sQ56sIIII48s"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
assert HEADER_SIZE == 192, f"Header struct must be 192 bytes, got {HEADER_SIZE}"


class OrderPacket(NamedTuple):
    order_id: int
    client_id: int
    price: int            # Fixed-point (int64)
    quantity: int
    side: int             # 0=Buy, 1=Sell
    order_type: int       # 0=Limit, 1=Market, 2=Cancel
    symbol_id: int
    timestamp_in_ns: int

    def pack(self) -> bytes:
        """Serialize order packet into exactly 64 raw bytes."""
        return struct.pack(
            ORDER_FORMAT,
            self.order_id,
            self.client_id,
            self.price,
            self.quantity,
            self.side,
            self.order_type,
            self.symbol_id,
            self.timestamp_in_ns,
            b"\x00" * 24
        )

    def pack_into(self, buffer, offset: int) -> None:
        """Zero-allocation write directly into a pre-allocated buffer/mmap slice."""
        struct.pack_into(
            ORDER_FORMAT,
            buffer,
            offset,
            self.order_id,
            self.client_id,
            self.price,
            self.quantity,
            self.side,
            self.order_type,
            self.symbol_id,
            self.timestamp_in_ns,
            b"\x00" * 24
        )

    @classmethod
    def unpack_from(cls, buffer, offset: int = 0) -> "OrderPacket":
        """Zero-copy read directly from a buffer/mmap slice."""
        fields = struct.unpack_from(ORDER_FORMAT, buffer, offset)
        return cls(
            order_id=fields[0],
            client_id=fields[1],
            price=fields[2],
            quantity=fields[3],
            side=fields[4],
            order_type=fields[5],
            symbol_id=fields[6],
            timestamp_in_ns=fields[7],
        )


class TradePacket(NamedTuple):
    trade_id: int
    buy_order_id: int
    sell_order_id: int
    price: int            # Fixed-point
    quantity: int
    symbol_id: int
    aggressor_side: int   # 0=Buy, 1=Sell
    match_timestamp_ns: int
    latency_ns: int

    def pack_into(self, buffer, offset: int) -> None:
        struct.pack_into(
            TRADE_FORMAT,
            buffer,
            offset,
            self.trade_id,
            self.buy_order_id,
            self.sell_order_id,
            self.price,
            self.quantity,
            self.symbol_id,
            self.aggressor_side,
            0, # reserved
            self.match_timestamp_ns,
            self.latency_ns,
            b"\x00" * 8
        )

    @classmethod
    def unpack_from(cls, buffer, offset: int = 0) -> "TradePacket":
        fields = struct.unpack_from(TRADE_FORMAT, buffer, offset)
        return cls(
            trade_id=fields[0],
            buy_order_id=fields[1],
            sell_order_id=fields[2],
            price=fields[3],
            quantity=fields[4],
            symbol_id=fields[5],
            aggressor_side=fields[6],
            match_timestamp_ns=fields[8],
            latency_ns=fields[9],
        )
