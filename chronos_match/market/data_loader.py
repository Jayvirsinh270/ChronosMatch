"""
ChronosMatch: Real Historical Market Data Loader & Symbol Catalog.

Discovers, parses, and indexes real-world stock and ETF historical datasets:
- US Equities & ETFs (NASDAQ / NYSE / AMEX: AAPL, NVDA, MSFT, TSLA, SPY, etc.)
- Indian Equities (BSE Top 100: Reliance, TCS, HDFC Bank, Infosys, etc.)
- Computes volatility, average volume, and price scaling for engine integration.
"""

from dataclasses import dataclass
import glob
import math
import os
from typing import Dict, List, Optional, Tuple, Any


@dataclass
class StockCandle:
    """Historical trading bar."""
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    adj_close: Optional[float] = None


@dataclass
class StockMetadata:
    """Discovered stock / instrument catalog entry."""
    ticker: str
    name: str
    exchange: str
    asset_type: str  # "STOCK" or "ETF"
    file_path: str
    first_date: str
    last_date: str
    total_bars: int
    last_close: float


class MarketDataLoader:
    """
    Catalog and high-performance reader for the 25,000+ historical stock datasets.
    """

    def __init__(self, data_root: str = "data"):
        self.data_root = data_root
        self._index: Dict[str, str] = {}  # ticker.upper() -> file_path
        self._cache: Dict[str, List[StockCandle]] = {}
        self._build_index()

    def _build_index(self) -> None:
        """Scan datasets and index available symbols."""
        if not os.path.exists(self.data_root):
            return

        # 1. Dataset 1 (stocks & etfs)
        ds1_stocks = os.path.join(self.data_root, "stock_marcate_data-set-1", "stocks")
        if os.path.exists(ds1_stocks):
            for fname in os.listdir(ds1_stocks):
                if fname.endswith(".csv"):
                    sym = fname[:-4].upper()
                    self._index[sym] = os.path.join(ds1_stocks, fname)

        ds1_etfs = os.path.join(self.data_root, "stock_marcate_data-set-1", "etfs")
        if os.path.exists(ds1_etfs):
            for fname in os.listdir(ds1_etfs):
                if fname.endswith(".csv"):
                    sym = fname[:-4].upper()
                    if sym not in self._index:
                        self._index[sym] = os.path.join(ds1_etfs, fname)

        # 2. Dataset 4 (BSE Top 100 Indian Equities)
        ds4_bse = os.path.join(self.data_root, "stock_marcate_data-set-4", "BSE_TOP100_25Oct2019TO06Dec2019")
        # Load code to company name mapping if present
        bse_meta = os.path.join(self.data_root, "stock_marcate_data-set-4", "BSE_TOP100_CompanyName_Company_Code_List.csv")
        bse_aliases: Dict[str, str] = {
            "500325": "RELIANCE",
            "532540": "TCS",
            "500180": "HDFCBANK",
            "500696": "HINDUNILVR",
            "500010": "HDFC",
            "532174": "ICICIBANK",
            "500247": "KOTAKBANK",
            "500209": "INFY",
            "500875": "ITC",
            "500790": "NESTLEIND",
        }
        if os.path.exists(ds4_bse):
            for fname in os.listdir(ds4_bse):
                if fname.endswith(".csv"):
                    code = fname[:-4].upper()
                    alias = bse_aliases.get(code, f"BSE_{code}")
                    fp = os.path.join(ds4_bse, fname)
                    self._index[alias] = fp
                    self._index[code] = fp

    def get_supported_tickers(self) -> List[str]:
        """Return list of all indexed tickers."""
        return sorted(list(self._index.keys()))

    def get_popular_companies(self) -> List[Dict[str, str]]:
        """Return curated list of high-profile global companies and indices."""
        popular = [
            {"ticker": "NVDA", "name": "NVIDIA Corporation", "market": "US Tech / AI"},
            {"ticker": "AAPL", "name": "Apple Inc.", "market": "US Tech"},
            {"ticker": "MSFT", "name": "Microsoft Corporation", "market": "US Tech"},
            {"ticker": "AMZN", "name": "Amazon.com Inc.", "market": "US Consumer / Cloud"},
            {"ticker": "TSLA", "name": "Tesla Inc.", "market": "US EV / Auto"},
            {"ticker": "SPY", "name": "SPDR S&P 500 ETF Trust", "market": "US Equities Benchmark"},
            {"ticker": "QQQ", "name": "Invesco QQQ Trust (Nasdaq 100)", "market": "US Tech Benchmark"},
            {"ticker": "RELIANCE", "name": "Reliance Industries Ltd", "market": "India BSE / NSE"},
            {"ticker": "TCS", "name": "Tata Consultancy Services", "market": "India Tech / IT"},
            {"ticker": "HDFCBANK", "name": "HDFC Bank Ltd", "market": "India Banking"},
            {"ticker": "INFY", "name": "Infosys Ltd", "market": "India Tech / IT"},
        ]
        return [p for p in popular if p["ticker"] in self._index]

    def load_candles(self, ticker: str, max_bars: int = 500) -> List[StockCandle]:
        """
        Load historical OHLCV bars for a ticker.
        Returns list ordered from earliest to latest.
        """
        sym = ticker.strip().upper()
        if sym in self._cache:
            candles = self._cache[sym]
            return candles[-max_bars:] if max_bars > 0 else candles

        if sym not in self._index:
            raise KeyError(f"Ticker '{sym}' not found in market dataset index.")

        fp = self._index[sym]
        candles: List[StockCandle] = []

        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            header_line = f.readline()
            headers = [h.strip() for h in header_line.split(",")]

            # Detect format: US (Date,Open,High,Low,Close,Adj Close,Volume) vs BSE (Date,Open Price,High Price,...)
            is_bse = "Open Price" in headers or "No.of Shares" in headers

            for line in f:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 5 or not parts[0]:
                    continue
                try:
                    if is_bse:
                        # BSE columns: Date,Open Price,High Price,Low Price,Close Price,WAP,No.of Shares
                        date_str = parts[0]
                        o = float(parts[1])
                        h = float(parts[2])
                        l = float(parts[3])
                        c = float(parts[4])
                        v = int(float(parts[6])) if len(parts) > 6 else 1000
                        adj = c
                    else:
                        # US columns: Date,Open,High,Low,Close,Adj Close,Volume
                        date_str = parts[0]
                        o = float(parts[1])
                        h = float(parts[2])
                        l = float(parts[3])
                        c = float(parts[4])
                        adj = float(parts[5]) if len(parts) > 5 else c
                        v = int(float(parts[6])) if len(parts) > 6 else 1000

                    candles.append(StockCandle(
                        date=date_str,
                        open=round(o, 2),
                        high=round(h, 2),
                        low=round(l, 2),
                        close=round(c, 2),
                        volume=v,
                        adj_close=round(adj, 2),
                    ))
                except (ValueError, IndexError):
                    continue

        # If BSE format, dates are in reverse order (newest first); reverse to chronological
        if is_bse and candles:
            candles.reverse()

        self._cache[sym] = candles
        return candles[-max_bars:] if max_bars > 0 else candles

    def compute_profile(self, ticker: str, lookback: int = 60) -> Dict[str, Any]:
        """
        Compute historical volatility, average daily volume, and typical price scale.
        """
        candles = self.load_candles(ticker, max_bars=lookback)
        if not candles:
            return {
                "ticker": ticker,
                "latest_price": 100.0,
                "volatility_daily": 0.02,
                "avg_volume": 100_000,
                "tick_size": 0.01,
            }

        latest_price = candles[-1].close
        returns: List[float] = []
        volumes: List[int] = []

        for i in range(1, len(candles)):
            prev_c = candles[i - 1].close
            curr_c = candles[i].close
            if prev_c > 0:
                returns.append(math.log(curr_c / prev_c))
            volumes.append(candles[i].volume)

        if returns:
            mean_ret = sum(returns) / len(returns)
            var = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
            daily_vol = math.sqrt(var)
        else:
            daily_vol = 0.02

        avg_vol = int(sum(volumes) / len(volumes)) if volumes else 100_000

        # High-priced stocks like BTC or high Indian stocks get appropriate tick sizes
        tick_size = 0.05 if "BSE" in ticker or ticker in ("RELIANCE", "TCS") else 0.01

        return {
            "ticker": ticker.upper(),
            "latest_price": latest_price,
            "volatility_daily": round(daily_vol, 4),
            "volatility_annualized": round(daily_vol * math.sqrt(252), 4),
            "avg_volume": avg_vol,
            "tick_size": tick_size,
            "total_bars": len(candles),
            "first_date": candles[0].date,
            "last_date": candles[-1].date,
        }
