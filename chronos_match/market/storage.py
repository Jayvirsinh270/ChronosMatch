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
        """Create tables for candles, ticks, ML regime snapshots, pattern events, and feature store."""
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
                pattern TEXT DEFAULT 'NORMAL',
                pattern_sentiment TEXT DEFAULT 'NEUTRAL',
                pattern_score REAL DEFAULT 0.0,
                rsi REAL DEFAULT 50.0,
                vwap REAL DEFAULT 0.0,
                created_at INTEGER NOT NULL
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_candles_sym_ts ON market_candles(symbol, timestamp);")

        # Migrate existing databases if columns are missing
        for col_def in [
            ("pattern", "TEXT DEFAULT 'NORMAL'"),
            ("pattern_sentiment", "TEXT DEFAULT 'NEUTRAL'"),
            ("pattern_score", "REAL DEFAULT 0.0"),
            ("rsi", "REAL DEFAULT 50.0"),
            ("vwap", "REAL DEFAULT 0.0"),
        ]:
            try:
                cur.execute(f"ALTER TABLE market_candles ADD COLUMN {col_def[0]} {col_def[1]};")
            except Exception:
                pass

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

        # Dedicated Candlestick Pattern Log
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_pattern_detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                date_str TEXT NOT NULL,
                pattern_name TEXT NOT NULL,
                category TEXT NOT NULL,
                sentiment TEXT NOT NULL,
                confidence REAL NOT NULL,
                pattern_score REAL NOT NULL,
                description TEXT NOT NULL
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pat_sym ON market_pattern_detections(symbol, timestamp);")

        # Institutional Feature Store Table (for future ML re-training & continuous learning)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_feature_store (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                date_str TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL,
                pattern TEXT NOT NULL,
                pattern_sentiment TEXT NOT NULL,
                pattern_score REAL NOT NULL,
                rsi14 REAL NOT NULL,
                macd_signal REAL NOT NULL,
                bb_bandwidth REAL NOT NULL,
                atr_ratio REAL NOT NULL,
                mom5 REAL NOT NULL,
                mom10 REAL NOT NULL,
                vol_ratio REAL NOT NULL,
                vol10 REAL NOT NULL,
                price_accel REAL NOT NULL,
                predicted_regime TEXT NOT NULL,
                confidence REAL NOT NULL,
                created_at INTEGER NOT NULL
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feat_sym ON market_feature_store(symbol, timestamp);")

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
        pattern: str = "NORMAL",
        pattern_sentiment: str = "NEUTRAL",
        pattern_score: float = 0.0,
        rsi: float = 50.0,
        vwap: float = 0.0,
    ) -> None:
        """Persist or upsert a candlestick bar with detected pattern metadata."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO market_candles (
                    symbol, timestamp, date_str, open, high, low, close, volume, trades_count,
                    pattern, pattern_sentiment, pattern_score, rsi, vwap, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                pattern,
                pattern_sentiment,
                round(pattern_score, 4),
                round(rsi, 2),
                round(vwap, 4),
                int(time.time()),
            ))
            self.conn.commit()
            cur.close()

    def record_pattern_detection(
        self,
        symbol: str,
        timestamp: int,
        date_str: str,
        pattern_name: str,
        category: str,
        sentiment: str,
        confidence: float,
        pattern_score: float,
        description: str,
    ) -> None:
        """Persist a detected candlestick pattern event."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO market_pattern_detections (
                    symbol, timestamp, date_str, pattern_name, category, sentiment, confidence, pattern_score, description
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol.upper(),
                timestamp,
                date_str,
                pattern_name,
                category,
                sentiment,
                round(confidence, 4),
                round(pattern_score, 4),
                description,
            ))
            self.conn.commit()
            cur.close()

    def record_feature_vector(
        self,
        symbol: str,
        timestamp: int,
        date_str: str,
        open_p: float,
        high_p: float,
        low_p: float,
        close_p: float,
        volume: int,
        pattern: str,
        pattern_sentiment: str,
        pattern_score: float,
        rsi14: float,
        macd_signal: float,
        bb_bandwidth: float,
        atr_ratio: float,
        mom5: float,
        mom10: float,
        vol_ratio: float,
        vol10: float,
        price_accel: float,
        predicted_regime: str,
        confidence: float,
    ) -> None:
        """Log full quantitative feature vector to the operational Feature Store."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO market_feature_store (
                    symbol, timestamp, date_str, open, high, low, close, volume,
                    pattern, pattern_sentiment, pattern_score,
                    rsi14, macd_signal, bb_bandwidth, atr_ratio, mom5, mom10,
                    vol_ratio, vol10, price_accel, predicted_regime, confidence, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol.upper(),
                timestamp,
                date_str,
                round(open_p, 4),
                round(high_p, 4),
                round(low_p, 4),
                round(close_p, 4),
                volume,
                pattern,
                pattern_sentiment,
                round(pattern_score, 4),
                round(rsi14, 2),
                round(macd_signal, 4),
                round(bb_bandwidth, 2),
                round(atr_ratio, 4),
                round(mom5, 4),
                round(mom10, 4),
                round(vol_ratio, 4),
                round(vol10, 4),
                round(price_accel, 4),
                predicted_regime,
                round(confidence, 4),
                int(time.time()),
            ))
            self.conn.commit()
            cur.close()

    def get_recent_patterns(self, symbol: str, limit: int = 15) -> List[Dict[str, Any]]:
        """Retrieve the most recent detected candlestick patterns for a symbol."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT symbol, timestamp, date_str, pattern_name, category, sentiment, confidence, pattern_score, description
                FROM market_pattern_detections
                WHERE symbol = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (symbol.upper(), limit))
            rows = cur.fetchall()
            cur.close()
            return [dict(r) for r in rows]

    def get_feature_store_samples(self, symbol: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        """Retrieve operational feature store vectors for training / backtesting."""
        with self._lock:
            cur = self.conn.cursor()
            if symbol:
                cur.execute("""
                    SELECT * FROM market_feature_store WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?
                """, (symbol.upper(), limit))
            else:
                cur.execute("""
                    SELECT * FROM market_feature_store ORDER BY timestamp DESC LIMIT ?
                """, (limit,))
            rows = cur.fetchall()
            cur.close()
            return [dict(r) for r in reversed(rows)]

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

            cur.execute("""
                SELECT COUNT(*) as pat_count FROM market_pattern_detections WHERE symbol = ?
            """, (symbol.upper(),))
            p_row = dict(cur.fetchone())

            cur.execute("""
                SELECT COUNT(*) as feat_count FROM market_feature_store WHERE symbol = ?
            """, (symbol.upper(),))
            f_row = dict(cur.fetchone())
            cur.close()

            return {
                "symbol": symbol.upper(),
                "persisted_candles": c_row["candle_count"],
                "persisted_ticks": t_row["tick_count"],
                "persisted_patterns": p_row["pat_count"],
                "persisted_features": f_row["feat_count"],
                "total_volume": c_row["total_volume"],
                "session_low": c_row["min_price"],
                "session_high": c_row["max_price"],
            }

    def close(self) -> None:
        """Close database connection cleanly."""
        with self._lock:
            self.conn.commit()
            self.conn.close()
