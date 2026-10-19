"""
ChronosMatch: Low-Latency Persistence & Asynchronous SQLite WAL Audit Ledger.
Stores all matched trade executions off the critical engine path using SQLite WAL mode.
Provides high-throughput batched commits and nanosecond-level historical trade queries.
"""

import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple, Any

from chronos_match.protocol import TradePacket, from_fixed_price
from chronos_match.ring_buffer import RingBuffer


class AuditLedger:
    """
    High-performance SQLite trade ledger configured for high-throughput write-ahead logging (WAL).
    """

    def __init__(self, db_path: str = "chronos_audit.db", read_only: bool = False):
        self.db_path = db_path
        self.read_only = read_only
        self._lock = threading.Lock()

        # Connect with autocommit disabled for explicit transaction control
        self.conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=30.0
        )
        self.conn.row_factory = sqlite3.Row

        if not read_only:
            self._configure_database()
            self._create_schema()

    def _configure_database(self) -> None:
        """Apply high-performance low-latency SQLite pragmas."""
        cur = self.conn.cursor()
        # WAL mode allows concurrent reads during writes without blocking
        cur.execute("PRAGMA journal_mode=WAL;")
        # NORMAL synchronicity is crash-safe in WAL mode while avoiding full fsync per transaction
        cur.execute("PRAGMA synchronous=NORMAL;")
        # In-memory temporary tables and indexes
        cur.execute("PRAGMA temp_store=MEMORY;")
        # 64 MB database page cache (-64000 KB)
        cur.execute("PRAGMA cache_size=-64000;")
        # Memory-mapped I/O up to 256 MB for lightning-fast reads
        cur.execute("PRAGMA mmap_size=268435456;")
        cur.close()

    def _create_schema(self) -> None:
        """Initialize the immutable trade ledger table and audit indices."""
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id INTEGER PRIMARY KEY,
                buy_order_id INTEGER NOT NULL,
                sell_order_id INTEGER NOT NULL,
                price INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                symbol_id INTEGER NOT NULL,
                aggressor_side INTEGER NOT NULL,
                match_timestamp_ns INTEGER NOT NULL,
                latency_ns INTEGER NOT NULL,
                recorded_at_ns INTEGER NOT NULL
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(match_timestamp_ns);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol_id);")
        self.conn.commit()
        cur.close()

    def insert_trade(self, trade: TradePacket) -> None:
        """Insert a single trade packet (for low-volume or testing)."""
        now_ns = time.perf_counter_ns()
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO trades (
                    trade_id, buy_order_id, sell_order_id, price, quantity,
                    symbol_id, aggressor_side, match_timestamp_ns, latency_ns, recorded_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.trade_id,
                    trade.buy_order_id,
                    trade.sell_order_id,
                    trade.price,
                    trade.quantity,
                    trade.symbol_id,
                    trade.aggressor_side,
                    trade.match_timestamp_ns,
                    trade.latency_ns,
                    now_ns,
                ),
            )
            self.conn.commit()
            cur.close()

    def insert_trades_batch(self, trades: List[TradePacket]) -> int:
        """
        Persist a batch of trade packets in a single atomic transaction.
        Returns the number of trades successfully inserted.
        """
        if not trades:
            return 0

        now_ns = time.perf_counter_ns()
        records = [
            (
                t.trade_id,
                t.buy_order_id,
                t.sell_order_id,
                t.price,
                t.quantity,
                t.symbol_id,
                t.aggressor_side,
                t.match_timestamp_ns,
                t.latency_ns,
                now_ns,
            )
            for t in trades
        ]

        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE;")
            cur.executemany(
                """
                INSERT OR IGNORE INTO trades (
                    trade_id, buy_order_id, sell_order_id, price, quantity,
                    symbol_id, aggressor_side, match_timestamp_ns, latency_ns, recorded_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
            self.conn.commit()
            count = cur.rowcount
            cur.close()

        return len(records)

    def get_trade_count(self) -> int:
        """Return total count of recorded trades in the ledger."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) FROM trades;")
            res = cur.fetchone()[0]
            cur.close()
            return res

    def get_trade_by_id(self, trade_id: int) -> Optional[Dict[str, Any]]:
        """Query a single trade record by trade_id."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM trades WHERE trade_id = ?;", (trade_id,))
            row = cur.fetchone()
            cur.close()
            if row is None:
                return None
            return self._row_to_dict(row)

    def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch the most recent N trades ordered by match timestamp descending."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT * FROM trades ORDER BY match_timestamp_ns DESC LIMIT ?;",
                (limit,)
            )
            rows = cur.fetchall()
            cur.close()
            return [self._row_to_dict(r) for r in rows]

    def get_trades_by_timerange(
        self, start_ns: int, end_ns: int, limit: int = 10_000
    ) -> List[Dict[str, Any]]:
        """Query trades executed within an exact nanosecond window."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                SELECT * FROM trades
                WHERE match_timestamp_ns >= ? AND match_timestamp_ns <= ?
                ORDER BY match_timestamp_ns ASC
                LIMIT ?;
                """,
                (start_ns, end_ns, limit),
            )
            rows = cur.fetchall()
            cur.close()
            return [self._row_to_dict(r) for r in rows]

    def get_summary(self) -> Dict[str, Any]:
        """Return summary statistics of the persisted ledger."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT
                    COUNT(*) as total_trades,
                    COALESCE(SUM(quantity), 0) as total_volume,
                    COALESCE(MIN(price), 0) as min_price_fixed,
                    COALESCE(MAX(price), 0) as max_price_fixed,
                    COALESCE(AVG(latency_ns), 0) as avg_latency_ns
                FROM trades;
            """)
            row = cur.fetchone()
            cur.close()
            return {
                "total_trades": row["total_trades"],
                "total_volume": row["total_volume"],
                "min_price": from_fixed_price(row["min_price_fixed"]),
                "max_price": from_fixed_price(row["max_price_fixed"]),
                "avg_latency_us": row["avg_latency_ns"] / 1000.0,
            }

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["price_float"] = from_fixed_price(d["price"])
        return d

    def close(self) -> None:
        """Safely close database connection with wal_checkpoint."""
        with self._lock:
            try:
                cur = self.conn.cursor()
                cur.execute("PRAGMA wal_checkpoint(PASSIVE);")
                cur.close()
                self.conn.close()
            except Exception:
                pass


class AuditLedgerWorker:
    """
    Asynchronous background worker draining trade packets from the outbound trade ring buffer
    and committing them in high-throughput batches to the SQLite WAL ledger.
    """

    def __init__(
        self,
        trade_buffer: RingBuffer,
        ledger: AuditLedger,
        batch_size: int = 2000,
        flush_interval_ms: float = 50.0,
    ):
        self.trade_buffer = trade_buffer
        self.ledger = ledger
        self.batch_size = batch_size
        self.flush_interval_s = flush_interval_ms / 1000.0

        self._pending_batch: List[TradePacket] = []
        self._last_flush_time = time.time()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        # Metrics
        self.total_trades_persisted = 0
        self.batches_committed = 0
        self.last_commit_duration_ms = 0.0

    def poll_and_buffer(self, max_read: int = 5000) -> int:
        """
        Read available trade packets from the ring buffer into the pending memory batch.
        Flushes to ledger if batch_size is reached.
        Returns count of trade packets read.
        """
        read_count = 0
        while read_count < max_read:
            trade = self.trade_buffer.read_trade()
            if trade is None:
                break
            self._pending_batch.append(trade)
            read_count += 1
            if len(self._pending_batch) >= self.batch_size:
                self.flush()

        # Check flush interval timeout
        now = time.time()
        if self._pending_batch and (now - self._last_flush_time) >= self.flush_interval_s:
            self.flush()

        return read_count

    def flush(self) -> int:
        """Force flush all currently pending trades to the SQLite WAL database."""
        if not self._pending_batch:
            return 0

        t0 = time.perf_counter()
        count = len(self._pending_batch)
        self.ledger.insert_trades_batch(self._pending_batch)
        t_duration_ms = (time.perf_counter() - t0) * 1000.0

        self.total_trades_persisted += count
        self.batches_committed += 1
        self.last_commit_duration_ms = t_duration_ms
        self._pending_batch.clear()
        self._last_flush_time = time.time()
        return count

    def start(self, poll_interval_s: float = 0.002) -> None:
        """Start asynchronous background persistence worker thread."""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return

        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._run_loop,
            args=(poll_interval_s,),
            name="ChronosAuditLedgerWorker",
            daemon=True,
        )
        self._worker_thread.start()

    def _run_loop(self, poll_interval_s: float) -> None:
        """Worker loop continuously polling ring buffer and batch persisting."""
        while not self._stop_event.is_set():
            drained = self.poll_and_buffer(max_read=5000)
            if drained == 0:
                time.sleep(poll_interval_s)

        # Final drain and flush upon shutdown
        self.drain_all()

    def drain_all(self) -> int:
        """Drain all remaining trades from buffer and flush to disk."""
        total = 0
        while True:
            trade = self.trade_buffer.read_trade()
            if trade is None:
                break
            self._pending_batch.append(trade)
            total += 1
            if len(self._pending_batch) >= self.batch_size:
                self.flush()
        if self._pending_batch:
            self.flush()
        return total

    def stop(self, timeout_s: float = 3.0) -> None:
        """Signal worker to stop and wait for thread to terminate after draining."""
        self._stop_event.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=timeout_s)
            self._worker_thread = None
        self.flush()
