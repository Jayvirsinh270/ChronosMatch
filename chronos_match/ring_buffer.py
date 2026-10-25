"""
Zero-Copy Lock-Free Single-Producer Single-Consumer (SPSC) Ring Buffer.
Uses memory-mapped file (mmap) for zero-copy inter-process communication.

Features:
- Cache-line padded atomic cursors (head & tail separated by 64 bytes).
- Bitwise fast modulo (capacity must be a power of 2).
- Zero-copy reads via memoryview slices.
- Direct binary packing without Python object overhead.
"""

import os
import mmap
import struct
from typing import Optional, Tuple
from pathlib import Path

from chronos_match.protocol import (
    HEADER_SIZE,
    ORDER_SIZE,
    OrderPacket,
    TradePacket,
)

# Header Offsets (3 cache lines of 64 bytes = 192 bytes total)
OFFSET_HEAD = 0           # uint64 (Producer cursor)
OFFSET_TAIL = 64          # uint64 (Consumer cursor, on Cache Line 2)
OFFSET_CAPACITY = 128     # uint32 (Cache Line 3)
OFFSET_MASK = 132         # uint32
OFFSET_SLOT_SIZE = 136    # uint32
OFFSET_IS_ACTIVE = 140    # uint32 (1 = Running, 0 = Stopped)

class RingBuffer:
    """
    Lock-free Single-Producer Single-Consumer (SPSC) shared memory ring buffer.
    """
    def __init__(
        self,
        filepath: str,
        capacity: int = 65536,  # Must be power of 2
        slot_size: int = ORDER_SIZE,
        create: bool = False
    ):
        self.filepath = Path(filepath)
        self.capacity = capacity
        self.slot_size = slot_size
        self._file = None
        self._mmap = None
        self._data_offset = HEADER_SIZE

        # Validate power of 2
        if (capacity & (capacity - 1)) != 0 or capacity <= 0:
            raise ValueError(f"Capacity must be a power of 2, got {capacity}")

        self.mask = capacity - 1
        self.total_size = HEADER_SIZE + (self.capacity * self.slot_size)

        if create:
            self._create_buffer()
        else:
            self._open_existing_buffer()

        self._buf = memoryview(self._mmap)

    def _create_buffer(self) -> None:
        """Create new shared memory file and initialize ring header."""
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        if self.filepath.exists():
            try:
                self.filepath.unlink()
            except Exception:
                pass
        # Open in w+b mode (create if not exist)
        self._file = open(self.filepath, "w+b")
        # Resize file to exact required byte size
        self._file.write(b"\x00" * self.total_size)
        self._file.flush()

        self._mmap = mmap.mmap(self._file.fileno(), self.total_size)

        # Write initial header metadata
        struct.pack_into(
            "=Q56sQ56sIIII48s",
            self._mmap,
            0,
            0,             # head = 0
            b"\x00" * 56,
            0,             # tail = 0
            b"\x00" * 56,
            self.capacity, # capacity
            self.mask,     # mask
            self.slot_size,# slot_size
            1,             # is_active = 1
            b"\x00" * 48
        )
        self._mmap.flush()

    def _open_existing_buffer(self) -> None:
        """Attach to an existing shared memory ring buffer."""
        if not self.filepath.exists():
            raise FileNotFoundError(f"Ring buffer file not found: {self.filepath}")

        self._file = open(self.filepath, "r+b")
        self._mmap = mmap.mmap(self._file.fileno(), 0)

        # Read configuration from header
        self.capacity, self.mask, self.slot_size, active = struct.unpack_from(
            "=IIII",
            self._mmap,
            OFFSET_CAPACITY
        )
        self.total_size = HEADER_SIZE + (self.capacity * self.slot_size)

    # --- Low-Latency Atomic Cursor Access ---

    @property
    def head(self) -> int:
        """Read producer write cursor."""
        return struct.unpack_from("=Q", self._mmap, OFFSET_HEAD)[0]

    @head.setter
    def head(self, val: int) -> None:
        struct.pack_into("=Q", self._mmap, OFFSET_HEAD, val)

    @property
    def tail(self) -> int:
        """Read consumer read cursor."""
        return struct.unpack_from("=Q", self._mmap, OFFSET_TAIL)[0]

    @tail.setter
    def tail(self, val: int) -> None:
        struct.pack_into("=Q", self._mmap, OFFSET_TAIL, val)

    @property
    def is_active(self) -> bool:
        return struct.unpack_from("=I", self._mmap, OFFSET_IS_ACTIVE)[0] == 1

    def stop(self) -> None:
        """Signal downstream consumers to stop."""
        struct.pack_into("=I", self._mmap, OFFSET_IS_ACTIVE, 0)

    # --- Producer Operations (Write) ---

    def is_full(self, current_head: Optional[int] = None) -> bool:
        """Check if ring buffer has no available slots."""
        h = current_head if current_head is not None else self.head
        t = self.tail
        return (h - t) >= self.capacity

    def write_order(self, order: OrderPacket) -> bool:
        """
        Write an OrderPacket into the next available slot.
        Returns True if successful, False if buffer is full (backpressure).
        """
        curr_head = self.head
        curr_tail = self.tail

        if (curr_head - curr_tail) >= self.capacity:
            return False  # Buffer full

        slot_index = curr_head & self.mask
        slot_offset = self._data_offset + (slot_index * self.slot_size)

        # Direct zero-allocation write into memoryview
        order.pack_into(self._mmap, slot_offset)

        # Advance write cursor (acts as release store)
        self.head = curr_head + 1
        return True

    def write_trade(self, trade: TradePacket) -> bool:
        """Write a TradePacket into the next available slot."""
        curr_head = self.head
        curr_tail = self.tail

        if (curr_head - curr_tail) >= self.capacity:
            return False

        slot_index = curr_head & self.mask
        slot_offset = self._data_offset + (slot_index * self.slot_size)
        trade.pack_into(self._mmap, slot_offset)
        self.head = curr_head + 1
        return True

    # --- Consumer Operations (Read) ---

    def is_empty(self, current_tail: Optional[int] = None) -> bool:
        """Check if all written messages have been consumed."""
        t = current_tail if current_tail is not None else self.tail
        return self.head == t

    def read_order(self) -> Optional[OrderPacket]:
        """
        Read the next OrderPacket from the ring buffer.
        Returns OrderPacket if available, None if empty.
        """
        curr_tail = self.tail
        curr_head = self.head

        if curr_tail == curr_head:
            return None  # Buffer empty

        slot_index = curr_tail & self.mask
        slot_offset = self._data_offset + (slot_index * self.slot_size)

        # Zero-copy unpack directly from memory
        order = OrderPacket.unpack_from(self._mmap, slot_offset)

        # Advance read cursor
        self.tail = curr_tail + 1
        return order

    def read_trade(self) -> Optional[TradePacket]:
        """Read the next TradePacket from the ring buffer."""
        curr_tail = self.tail
        curr_head = self.head

        if curr_tail == curr_head:
            return None

        slot_index = curr_tail & self.mask
        slot_offset = self._data_offset + (slot_index * self.slot_size)
        trade = TradePacket.unpack_from(self._mmap, slot_offset)
        self.tail = curr_tail + 1
        return trade

    def read_order_raw_view(self) -> Optional[Tuple[memoryview, int]]:
        """
        ZERO-COPY: Return a direct memoryview slice of the slot without unpacking into a Python object!
        Returns (memoryview_slice, new_tail) or None if empty.
        The consumer can inspect raw bytes and advance tail when done.
        """
        curr_tail = self.tail
        if curr_tail == self.head:
            return None

        slot_index = curr_tail & self.mask
        slot_offset = self._data_offset + (slot_index * self.slot_size)
        view = self._buf[slot_offset : slot_offset + self.slot_size]
        return view, curr_tail + 1

    def advance_tail(self, new_tail: int) -> None:
        """Advance consumer cursor after processing zero-copy slice."""
        self.tail = new_tail

    def close(self) -> None:
        """Flush and close memory map and file handles."""
        if self._buf is not None:
            self._buf.release()
            self._buf = None
        if self._mmap is not None:
            self._mmap.flush()
            self._mmap.close()
            self._mmap = None
        if self._file is not None:
            self._file.close()
            self._file = None
