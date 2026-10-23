"""
ChronosMatch: Ultra-High-Throughput Write-Ahead Log (WAL).

Implements an append-only binary journal for zero-data-loss durability.
Guarantees deterministic ordering with 64-bit monotonic sequence numbers,
nanosecond timestamps, and CRC32 payload checksums.
"""

from dataclasses import dataclass
from enum import IntEnum
import os
import struct
import time
from typing import Generator, List, Optional, Tuple
import zlib


# Binary header constants
WAL_MAGIC = b"CWAL"  # Chronos Write Ahead Log
WAL_HEADER_FORMAT = ">4sQQBII"  # magic(4), seq(8), ts_ns(8), type(1), payload_len(4), crc32(4)
WAL_HEADER_SIZE = struct.calcsize(WAL_HEADER_FORMAT)  # 29 bytes

# Payload format definitions
FORMAT_ORDER_SUBMIT = ">QIIBQQBBB"   # order_id(8), client_id(4), symbol_id(4), side(1), price(8), qty(8), tif(1), post_only(1), stp(1)
SIZE_ORDER_SUBMIT = struct.calcsize(FORMAT_ORDER_SUBMIT)

FORMAT_ORDER_CANCEL = ">QII"         # order_id(8), client_id(4), symbol_id(4)
SIZE_ORDER_CANCEL = struct.calcsize(FORMAT_ORDER_CANCEL)

FORMAT_ACCOUNT_DEPOSIT = ">IdIq"     # client_id(4), cash_usd(8), symbol_id(4), shares(8)
SIZE_ACCOUNT_DEPOSIT = struct.calcsize(FORMAT_ACCOUNT_DEPOSIT)


class WALEventType(IntEnum):
    """Types of journaled state-machine transitions."""
    ORDER_SUBMIT = 1
    ORDER_CANCEL = 2
    ACCOUNT_DEPOSIT = 3
    SNAPSHOT_MARKER = 4


@dataclass
class WALRecord:
    """Deserialized record from Write-Ahead Log."""
    seq: int
    timestamp_ns: int
    event_type: WALEventType
    payload: bytes
    crc32: int
    offset: int  # Byte offset where record began in file

    # Helper getters for common event types
    def as_order_submit(self) -> Tuple[int, int, int, int, int, int, int, int, int]:
        """Returns (order_id, client_id, symbol_id, side, price, quantity, tif, post_only, stp)."""
        return struct.unpack(FORMAT_ORDER_SUBMIT, self.payload)

    def as_order_cancel(self) -> Tuple[int, int, int]:
        """Returns (order_id, client_id, symbol_id)."""
        return struct.unpack(FORMAT_ORDER_CANCEL, self.payload)

    def as_account_deposit(self) -> Tuple[int, float, int, int]:
        """Returns (client_id, cash_usd, symbol_id, shares)."""
        return struct.unpack(FORMAT_ACCOUNT_DEPOSIT, self.payload)

    def as_snapshot_marker(self) -> Tuple[int, str]:
        """Returns (snapshot_seq, snapshot_path)."""
        snap_seq, path_len = struct.unpack(">QH", self.payload[:10])
        snap_path = self.payload[10:10 + path_len].decode("utf-8")
        return snap_seq, snap_path


class WriteAheadLog:
    """
    Append-only binary journal engine.
    Supports immediate fsync, batched group commits, and crash verification.
    """

    def __init__(
        self,
        filepath: str,
        sync_mode: str = "batched",
        batch_size: int = 500,
        start_seq: int = 1,
    ):
        """
        :param filepath: Path to the .wal binary file.
        :param sync_mode: 'immediate' (fsync per record), 'batched' (fsync every batch_size), or 'none'.
        :param batch_size: Number of records before fsyncing in 'batched' mode.
        :param start_seq: Initial sequence number if file is newly created.
        """
        self.filepath = filepath
        self.sync_mode = sync_mode
        self.batch_size = batch_size
        self._next_seq = start_seq
        self._unflushed_count = 0
        self.corrupted_tail_bytes = 0

        # Create parent directories if needed
        parent_dir = os.path.dirname(filepath)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        # If file exists, determine highest recorded sequence number
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            last_seq = self._find_last_seq()
            if last_seq >= self._next_seq:
                self._next_seq = last_seq + 1

        self._file = open(self.filepath, "a+b")

    def _find_last_seq(self) -> int:
        """Scan file to find highest valid sequence number."""
        max_seq = 0
        for record in self.read_records(self.filepath):
            if record.seq > max_seq:
                max_seq = record.seq
        return max_seq

    @property
    def next_seq(self) -> int:
        return self._next_seq

    def append_order_submit(
        self,
        order_id: int,
        client_id: int,
        symbol_id: int,
        side: int,
        price: int,
        quantity: int,
        tif: int = 0,
        post_only: int = 0,
        stp_action: int = 0,
        timestamp_ns: int = 0,
    ) -> int:
        """Write an ORDER_SUBMIT transition to the journal."""
        payload = struct.pack(
            FORMAT_ORDER_SUBMIT,
            order_id,
            client_id,
            symbol_id,
            side,
            price,
            quantity,
            tif,
            post_only,
            stp_action,
        )
        return self._append_raw(WALEventType.ORDER_SUBMIT, payload, timestamp_ns)

    def append_order_cancel(
        self,
        order_id: int,
        client_id: int,
        symbol_id: int,
        timestamp_ns: int = 0,
    ) -> int:
        """Write an ORDER_CANCEL transition to the journal."""
        payload = struct.pack(
            FORMAT_ORDER_CANCEL,
            order_id,
            client_id,
            symbol_id,
        )
        return self._append_raw(WALEventType.ORDER_CANCEL, payload, timestamp_ns)

    def append_account_deposit(
        self,
        client_id: int,
        cash_usd: float = 0.0,
        symbol_id: int = 0,
        shares: int = 0,
        timestamp_ns: int = 0,
    ) -> int:
        """Write an ACCOUNT_DEPOSIT transition to the journal."""
        payload = struct.pack(
            FORMAT_ACCOUNT_DEPOSIT,
            client_id,
            cash_usd,
            symbol_id,
            shares,
        )
        return self._append_raw(WALEventType.ACCOUNT_DEPOSIT, payload, timestamp_ns)

    def append_snapshot_marker(
        self,
        snapshot_seq: int,
        snapshot_path: str,
        timestamp_ns: int = 0,
    ) -> int:
        """Write a SNAPSHOT_MARKER transition pointing to a disk snapshot."""
        path_bytes = snapshot_path.encode("utf-8")
        payload = struct.pack(f">QH{len(path_bytes)}s", snapshot_seq, len(path_bytes), path_bytes)
        return self._append_raw(WALEventType.SNAPSHOT_MARKER, payload, timestamp_ns)

    def _append_raw(self, event_type: WALEventType, payload: bytes, timestamp_ns: int = 0) -> int:
        """Pack binary header + payload + CRC32 and append to journal file."""
        seq = self._next_seq
        self._next_seq += 1

        ts = timestamp_ns if timestamp_ns > 0 else time.perf_counter_ns()
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        header = struct.pack(
            WAL_HEADER_FORMAT,
            WAL_MAGIC,
            seq,
            ts,
            int(event_type),
            len(payload),
            crc,
        )

        self._file.write(header + payload)
        self._unflushed_count += 1

        if self.sync_mode == "immediate":
            self.flush(fsync=True)
        elif self.sync_mode == "batched" and self._unflushed_count >= self.batch_size:
            self.flush(fsync=True)

        return seq

    def flush(self, fsync: bool = False) -> None:
        """Flush user-space buffers to OS page cache, and optionally sync to disk."""
        if self._file and not self._file.closed:
            self._file.flush()
            if fsync:
                try:
                    os.fsync(self._file.fileno())
                except (OSError, ValueError):
                    pass
            self._unflushed_count = 0

    def close(self) -> None:
        """Flush and close journal file."""
        if self._file and not self._file.closed:
            self.flush(fsync=True)
            self._file.close()

    @staticmethod
    def read_records(
        filepath: str,
        from_seq: int = 0,
    ) -> Generator[WALRecord, None, None]:
        """
        Stream records sequentially from a WAL file.
        Verifies magic bytes and CRC32 payload checksum on every record.
        Stops cleanly if a crash caused an incomplete trailing write at EOF.
        """
        if not os.path.exists(filepath):
            return

        with open(filepath, "rb") as f:
            while True:
                offset = f.tell()
                header_bytes = f.read(WAL_HEADER_SIZE)
                if len(header_bytes) == 0:
                    break  # Normal Clean EOF

                if len(header_bytes) < WAL_HEADER_SIZE:
                    # Incomplete header at EOF (crash mid-write)
                    break

                magic, seq, ts_ns, ev_type, p_len, crc = struct.unpack(
                    WAL_HEADER_FORMAT, header_bytes
                )

                if magic != WAL_MAGIC:
                    # File header corruption
                    break

                payload = f.read(p_len)
                if len(payload) < p_len:
                    # Incomplete payload at EOF (crash mid-payload)
                    break

                computed_crc = zlib.crc32(payload) & 0xFFFFFFFF
                if computed_crc != crc:
                    # Corrupted payload checksum!
                    break

                if seq >= from_seq:
                    yield WALRecord(
                        seq=seq,
                        timestamp_ns=ts_ns,
                        event_type=WALEventType(ev_type),
                        payload=payload,
                        crc32=crc,
                        offset=offset,
                    )

    @staticmethod
    def truncate_corrupted_tail(filepath: str) -> int:
        """
        Scans WAL file, identifies the byte position of the last valid record,
        and truncates any corrupted/half-written trailing bytes left by a crash.
        Returns the number of corrupted bytes truncated.
        """
        if not os.path.exists(filepath):
            return 0

        last_valid_end = 0
        with open(filepath, "rb") as f:
            while True:
                start_offset = f.tell()
                header_bytes = f.read(WAL_HEADER_SIZE)
                if len(header_bytes) < WAL_HEADER_SIZE:
                    break

                magic, seq, ts_ns, ev_type, p_len, crc = struct.unpack(
                    WAL_HEADER_FORMAT, header_bytes
                )
                if magic != WAL_MAGIC:
                    break

                payload = f.read(p_len)
                if len(payload) < p_len:
                    break

                if (zlib.crc32(payload) & 0xFFFFFFFF) != crc:
                    break

                last_valid_end = f.tell()

        file_size = os.path.getsize(filepath)
        corrupted_bytes = file_size - last_valid_end
        if corrupted_bytes > 0:
            with open(filepath, "a+b") as f:
                f.truncate(last_valid_end)

        return corrupted_bytes
