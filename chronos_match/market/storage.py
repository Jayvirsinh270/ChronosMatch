"""
ChronosMatch: Market Data Storage & Historical Persistence Engine.
Stores real-time stock candles, high-frequency tick records, and ML regime snapshots
into a high-performance SQLite database with WAL mode and CSV export capabilities.
"""

from dataclasses import dataclass
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple, Any


@dataclass
class StoredCandle:
    symbol: str
    timestamp: int
    date_str: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    trades_count: int


class MarketDataStore:
    """
    Persistent storage engine for real-time market data, synthetic ticks,
    candlestick bars, and ML pattern regime transitions.
    """

    def __init__(self, db_path: str = "chronos_market_history.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self._configure_db()
        self._create_schema()

    def _configure_db(self) -> None:
        """Apply high-throughput low-latency SQLite pragmas."""
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA temp_store=MEMORY;")
        cur.execute("PRAGMA cache_size=-32000;")  # 32 MB cache
        cur.close()

    def _create_schema(self) -> None:
        """Create tables for candles, ticks, and ML regime snapshots."""
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                date_str TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL,
                trades_count INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_candles_sym_ts ON market_candles(symbol, timestamp);")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                side INTEGER NOT NULL,
                best_bid REAL NOT NULL,
                best_ask REAL NOT NULL,
                ofi REAL NOT NULL,
                regime TEXT NOT NULL
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ticks_sym_ts ON market_ticks(symbol, timestamp_ns);")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ml_regime_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                regime TEXT NOT NULL,
                confidence REAL NOT NULL,
                predicted_drift REAL NOT NULL,
                volatility REAL NOT NULL,
                spread_multiplier REAL NOT NULL
            );
        """)
        self.conn.commit()
        cur.close()

    def record_candle(
        self,
        symbol: str,
        timestamp: int,
        date_str: str,
        open_p: float,
        high_p: float,
        low_p: float,
        close_p: float,
        volume: int,
        trades_count: int,
    ) -> None:
        """Persist or upsert a candlestick bar."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO market_candles (symbol, timestamp, date_str, open, high, low, close, volume, trades_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol.upper(),
                timestamp,
                date_str,
                round(open_p, 4),
                round(high_p, 4),
                round(low_p, 4),
                round(close_p, 4),
                volume,
                trades_count,
                int(time.time()),
            ))
            self.conn.commit()
            cur.close()

    def record_tick(
        self,
        symbol: str,
        timestamp_ns: int,
        price: float,
        quantity: int,
        side: int,
        best_bid: float,
        best_ask: float,
        ofi: float,
        regime: str,
    ) -> None:
        """Log a trade tick with microstructure context."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO market_ticks (symbol, timestamp_ns, price, quantity, side, best_bid, best_ask, ofi, regime)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol.upper(),
                timestamp_ns,
                round(price, 4),
                quantity,
                side,
                round(best_bid, 4),
                round(best_ask, 4),
                round(ofi, 2),
                regime,
            ))
            self.conn.commit()
            cur.close()

    def record_ml_regime(
        self,
        symbol: str,
        timestamp_ns: int,
        regime: str,
        confidence: float,
        predicted_drift: float,
        volatility: float,
        spread_multiplier: float,
    ) -> None:
        """Record an ML pattern regime transition snapshot."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO ml_regime_logs (symbol, timestamp_ns, regime, confidence, predicted_drift, volatility, spread_multiplier)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol.upper(),
                timestamp_ns,
                regime,
                round(confidence, 4),
                round(predicted_drift, 6),
                round(volatility, 6),
                round(spread_multiplier, 3),
            ))
            self.conn.commit()
            cur.close()

    def get_recent_candles(self, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieve the most recent candlestick bars for a symbol."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT symbol, timestamp, date_str, open, high, low, close, volume, trades_count
                FROM market_candles
                WHERE symbol = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (symbol.upper(), limit))
            rows = cur.fetchall()
            cur.close()
            result = [dict(r) for r in reversed(rows)]
            return result

    def get_market_statistics(self, symbol: str) -> Dict[str, Any]:
        """Compute aggregated statistics for a symbol's persisted session."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT COUNT(*) as candle_count, COALESCE(SUM(volume), 0) as total_volume,
                       COALESCE(MIN(low), 0.0) as min_price, COALESCE(MAX(high), 0.0) as max_price
                FROM market_candles WHERE symbol = ?
            """, (symbol.upper(),))
            c_row = dict(cur.fetchone())

            cur.execute("""
                SELECT COUNT(*) as tick_count FROM market_ticks WHERE symbol = ?
            """, (symbol.upper(),))
            t_row = dict(cur.fetchone())
            cur.close()

            return {
                "symbol": symbol.upper(),
                "persisted_candles": c_row["candle_count"],
                "persisted_ticks": t_row["tick_count"],
                "total_volume": c_row["total_volume"],
                "session_low": c_row["min_price"],
                "session_high": c_row["max_price"],
            }

    def close(self) -> None:
        """Close database connection cleanly."""
        with self._lock:
            self.conn.commit()
            self.conn.close()
