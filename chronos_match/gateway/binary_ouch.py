"""
ChronosMatch: Ultra-Low-Latency Binary OUCH/ITCH Gateway Protocol.

Implements compact binary framing based on the industry-standard NASDAQ OUCH/ITCH specification.
Eliminates string encoding and delimiter parsing overhead for sub-microsecond algorithmic order flow.
"""

from dataclasses import dataclass
import struct
import time
from typing import Optional, Tuple, Union


# Inbound OUCH Message Types
OUCH_ENTER_ORDER = b"O"
OUCH_CANCEL_ORDER = b"X"

# Outbound ITCH Message Types
ITCH_ORDER_ACCEPTED = b"A"
ITCH_ORDER_EXECUTED = b"E"
ITCH_ORDER_CANCELED = b"C"
ITCH_ORDER_REJECTED = b"J"

# Binary Struct Formats
FORMAT_ENTER_ORDER = ">cQcI4sQ"    # type(1), token(8), side(1), shares(4), stock(4), price(8)
SIZE_ENTER_ORDER = struct.calcsize(FORMAT_ENTER_ORDER)  # 26 bytes

FORMAT_CANCEL_ORDER = ">cQI"       # type(1), token(8), shares(4)
SIZE_CANCEL_ORDER = struct.calcsize(FORMAT_CANCEL_ORDER)  # 13 bytes

FORMAT_ORDER_ACCEPTED = ">cQQQcI4sQ"  # type(1), ts(8), token(8), order_ref(8), side(1), shares(4), stock(4), price(8)
SIZE_ORDER_ACCEPTED = struct.calcsize(FORMAT_ORDER_ACCEPTED)  # 42 bytes

FORMAT_ORDER_EXECUTED = ">cQQIQ"     # type(1), ts(8), order_ref(8), executed_shares(4), match_num(8)
SIZE_ORDER_EXECUTED = struct.calcsize(FORMAT_ORDER_EXECUTED)  # 29 bytes

FORMAT_ORDER_CANCELED = ">cQQI"      # type(1), ts(8), order_ref(8), canceled_shares(4)
SIZE_ORDER_CANCELED = struct.calcsize(FORMAT_ORDER_CANCELED)  # 21 bytes

FORMAT_ORDER_REJECTED = ">cQc"       # type(1), token(8), reason_code(1)
SIZE_ORDER_REJECTED = struct.calcsize(FORMAT_ORDER_REJECTED)  # 10 bytes


@dataclass
class OuchEnterOrder:
    order_token: int
    side: str        # 'B' or 'S'
    shares: int
    stock: str       # e.g. 'SOL ', 'BTC '
    price: int       # fixed-price representation


@dataclass
class OuchCancelOrder:
    order_token: int
    shares: int


class BinaryOuchCodec:
    """
    High-speed binary serializer and deserializer for OUCH/ITCH messages.
    """

    @staticmethod
    def pack_enter_order(token: int, side: str, shares: int, stock: str, price: int) -> bytes:
        stock_bytes = stock.encode("ascii")[:4].ljust(4, b" ")
        side_bytes = side.encode("ascii")[:1]
        return struct.pack(
            FORMAT_ENTER_ORDER,
            OUCH_ENTER_ORDER,
            token,
            side_bytes,
            shares,
            stock_bytes,
            price,
        )

    @staticmethod
    def unpack_enter_order(data: bytes) -> OuchEnterOrder:
        msg_type, token, side_b, shares, stock_b, price = struct.unpack(FORMAT_ENTER_ORDER, data)
        return OuchEnterOrder(
            order_token=token,
            side=side_b.decode("ascii"),
            shares=shares,
            stock=stock_b.decode("ascii").strip(),
            price=price,
        )

    @staticmethod
    def pack_cancel_order(token: int, shares: int) -> bytes:
        return struct.pack(
            FORMAT_CANCEL_ORDER,
            OUCH_CANCEL_ORDER,
            token,
            shares,
        )

    @staticmethod
    def unpack_cancel_order(data: bytes) -> OuchCancelOrder:
        msg_type, token, shares = struct.unpack(FORMAT_CANCEL_ORDER, data)
        return OuchCancelOrder(order_token=token, shares=shares)

    @staticmethod
    def pack_order_accepted(token: int, order_ref: int, side: str, shares: int, stock: str, price: int, timestamp_ns: int = 0) -> bytes:
        ts = timestamp_ns if timestamp_ns > 0 else time.perf_counter_ns()
        stock_b = stock.encode("ascii")[:4].ljust(4, b" ")
        side_b = side.encode("ascii")[:1]
        return struct.pack(
            FORMAT_ORDER_ACCEPTED,
            ITCH_ORDER_ACCEPTED,
            ts,
            token,
            order_ref,
            side_b,
            shares,
            stock_b,
            price,
        )

    @staticmethod
    def pack_order_executed(order_ref: int, executed_shares: int, match_num: int, timestamp_ns: int = 0) -> bytes:
        ts = timestamp_ns if timestamp_ns > 0 else time.perf_counter_ns()
        return struct.pack(
            FORMAT_ORDER_EXECUTED,
            ITCH_ORDER_EXECUTED,
            ts,
            order_ref,
            executed_shares,
            match_num,
        )

    @staticmethod
    def pack_order_canceled(order_ref: int, canceled_shares: int, timestamp_ns: int = 0) -> bytes:
        ts = timestamp_ns if timestamp_ns > 0 else time.perf_counter_ns()
        return struct.pack(
            FORMAT_ORDER_CANCELED,
            ITCH_ORDER_CANCELED,
            ts,
            order_ref,
            canceled_shares,
        )
