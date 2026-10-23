"""
ChronosMatch: Multi-Symbol & Multi-Asset Matching Router.

Manages sharded, isolated matching venues per financial instrument:
- Independent FastOrderBook instances per symbol
- Per-symbol tick sizes, price scales, and distribution generators
- Cross-instrument execution telemetry and Level 2 depth snapshotting
- Unified routing by integer symbol_id or string ticker (e.g., 'BTC-USD')
"""

from dataclasses import dataclass, field
import random
import time
from typing import Dict, List, Optional, Any, Union

from chronos_match.protocol import (
    OrderSide,
    OrderType,
    to_fixed_price,
    from_fixed_price,
    TradePacket,
)
from chronos_match.engine.order_book_cy import FastOrderBook
from chronos_match.engine.order_policies import (
    OrderPolicyEngine,
    TimeInForce,
    SelfTradeAction,
)
from chronos_match.monitor.metrics import LatencyProfiler
from chronos_match.monitor.whale_detector import WhaleDetector
from chronos_match.market.distribution import OrderDistributionGenerator


@dataclass
class SymbolInfo:
    """Static metadata for a tradeable financial instrument."""
    symbol_id: int
    ticker: str
    base_asset: str
    quote_asset: str = "USD"
    tick_size: float = 0.01
    lot_size: int = 1
    initial_price: float = 150.00
    price_scale: int = 10_000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "ticker": self.ticker,
            "base_asset": self.base_asset,
            "quote_asset": self.quote_asset,
            "tick_size": self.tick_size,
            "lot_size": self.lot_size,
            "initial_price": self.initial_price,
        }


class SingleSymbolVenue:
    """
    Isolated execution venue and matching core for a single financial instrument.
    Contains dedicated FastOrderBook, policy engine, profiler, and tick stream.
    """

    def __init__(
        self,
        info: SymbolInfo,
        client_order_registry: Dict[int, int],
        arena_capacity: int = 100_000,
    ):
        self.info = info
        self.book = FastOrderBook(arena_capacity=arena_capacity)
        self.policy_engine = OrderPolicyEngine(
            book=self.book,
            client_order_registry=client_order_registry,
        )
        self.profiler = LatencyProfiler(window_size=50_000)
        self.whale_detector = WhaleDetector()
        self.generator = OrderDistributionGenerator(
            symbol_id=info.symbol_id,
            initial_price=info.initial_price,
            tick_size=info.tick_size,
        )

        self.total_orders = 0
        self.total_trades = 0
        self.total_volume = 0
        self.recent_trades: List[Dict[str, Any]] = []
        self.price_history: List[Dict[str, Any]] = []
        self.user_open_orders: Dict[int, Dict[str, Any]] = {}

        self.last_trade_price = info.initial_price
        self.high_price = info.initial_price * 1.002
        self.low_price = info.initial_price * 0.998
        self.cumulative_notional = 0.0

    def seed_liquidity(self, order_id_start: int, client_order_registry: Dict[int, int]) -> int:
        """Pre-seed 8 bids and 8 asks around symbol initial price."""
        next_id = order_id_start
        tick = self.info.tick_size
        base_bid = self.info.initial_price - tick
        base_ask = self.info.initial_price + tick

        # Seed Bids
        for i in range(8):
            p = base_bid - (i * tick * 5)
            self.book.add_limit_order(
                order_id=next_id,
                client_id=99,
                price=to_fixed_price(p),
                quantity=random.choice([100, 250, 500, 1000]),
                side=int(OrderSide.BUY),
                symbol_id=self.info.symbol_id,
                timestamp_ns=time.perf_counter_ns(),
            )
            client_order_registry[next_id] = 99
            next_id += 1
            self.total_orders += 1

        # Seed Asks
        for i in range(8):
            p = base_ask + (i * tick * 5)
            self.book.add_limit_order(
                order_id=next_id,
                client_id=99,
                price=to_fixed_price(p),
                quantity=random.choice([100, 250, 500, 1000]),
                side=int(OrderSide.SELL),
                symbol_id=self.info.symbol_id,
                timestamp_ns=time.perf_counter_ns(),
            )
            client_order_registry[next_id] = 99
            next_id += 1
            self.total_orders += 1

        return next_id

    def get_snapshot(self) -> Dict[str, Any]:
        """Generate real-time snapshot for this instrument."""
        raw_l2 = self.book.get_l2_snapshot(depth=10)
        stats = self.profiler.compute_stats()

        bids = [
            {"price": from_fixed_price(b["price"]), "volume": b["volume"], "order_count": b["order_count"]}
            for b in raw_l2["bids"]
        ]
        asks = [
            {"price": from_fixed_price(a["price"]), "volume": a["volume"], "order_count": a["order_count"]}
            for a in raw_l2["asks"]
        ]

        best_bid = from_fixed_price(self.book.best_bid_price)
        best_ask = from_fixed_price(self.book.best_ask_price)
        spread = from_fixed_price(self.book.spread)

        total_bid_vol = sum(b["volume"] for b in bids)
        total_ask_vol = sum(a["volume"] for a in asks)
        denom = total_bid_vol + total_ask_vol
        imbalance = ((total_bid_vol - total_ask_vol) / denom) if denom > 0 else 0.0
        vwap = (self.cumulative_notional / max(1, self.total_volume)) if self.total_volume > 0 else self.last_trade_price

        recent_whales = [
            a.to_dict() for a in self.whale_detector.get_recent_alerts(limit=5)
        ]

        return {
            "symbol": self.info.ticker,
            "symbol_id": self.info.symbol_id,
            "base_asset": self.info.base_asset,
            "quote_asset": self.info.quote_asset,
            "tick_size": self.info.tick_size,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "last_price": round(self.last_trade_price, 2),
            "high_24h": round(self.high_price, 2),
            "low_24h": round(self.low_price, 2),
            "vwap": round(vwap, 2),
            "imbalance": round(imbalance, 3),
            "total_bid_vol": total_bid_vol,
            "total_ask_vol": total_ask_vol,
            "l2": {"bids": bids, "asks": asks},
            "recent_trades": self.recent_trades[:20],
            "price_history": self.price_history[-50:],
            "open_orders": list(self.user_open_orders.values()),
            "whale_alerts": recent_whales,
            "total_orders": self.total_orders,
            "total_trades": self.total_trades,
            "total_volume": self.total_volume,
            "stats": stats._asdict() if stats else None,
        }


class MultiBookRouter:
    """
    Central Exchange Router distributing order flow across multiple symbol venues.
    Maintains cross-asset registry, shared STP directory, and aggregate statistics.
    """

    def __init__(self):
        self.venues_by_id: Dict[int, SingleSymbolVenue] = {}
        self.venues_by_ticker: Dict[str, SingleSymbolVenue] = {}
        self.client_order_registry: Dict[int, int] = {}
        self.order_id_counter = 1

    def register_symbol(self, info: SymbolInfo, arena_capacity: int = 100_000, seed: bool = True) -> SingleSymbolVenue:
        """Register a new instrument and spin up its isolated matching core."""
        venue = SingleSymbolVenue(
            info=info,
            client_order_registry=self.client_order_registry,
            arena_capacity=arena_capacity,
        )
        self.venues_by_id[info.symbol_id] = venue
        self.venues_by_ticker[info.ticker.upper()] = venue
        # Also map base_asset alias (e.g. 'SOL' -> 'SOL-USD')
        self.venues_by_ticker[info.base_asset.upper()] = venue

        if seed:
            self.order_id_counter = venue.seed_liquidity(
                order_id_start=self.order_id_counter,
                client_order_registry=self.client_order_registry,
            )
        return venue

    def get_venue(self, symbol: Union[int, str]) -> SingleSymbolVenue:
        """Retrieve execution venue by symbol_id (int) or ticker (str)."""
        if isinstance(symbol, int):
            if symbol not in self.venues_by_id:
                raise KeyError(f"Unknown symbol_id: {symbol}")
            return self.venues_by_id[symbol]

        sym_str = str(symbol).strip().upper()
        # Normalization (e.g. 'BTC/USD' -> 'BTC-USD')
        sym_str = sym_str.replace("/", "-")
        if sym_str in self.venues_by_ticker:
            return self.venues_by_ticker[sym_str]

        # Check fallback defaults
        if sym_str in ("CHRONOS", "DEFAULT", "SOL", "SOL-USD"):
            return self.venues_by_id.get(1, list(self.venues_by_id.values())[0])

        raise KeyError(f"Unknown symbol ticker: '{symbol}'. Available: {list(self.venues_by_ticker.keys())}")

    def list_symbols(self) -> List[Dict[str, Any]]:
        """Return metadata for all registered tradeable symbols."""
        return [venue.info.to_dict() for venue in self.venues_by_id.values()]

    def get_all_tickers(self) -> List[Dict[str, Any]]:
        """Return 24h ticker summary across all registered symbols."""
        tickers = []
        for venue in self.venues_by_id.values():
            best_bid = from_fixed_price(venue.book.best_bid_price)
            best_ask = from_fixed_price(venue.book.best_ask_price)
            tickers.append({
                "symbol": venue.info.ticker,
                "symbol_id": venue.info.symbol_id,
                "last_price": round(venue.last_trade_price, 2),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "high_24h": round(venue.high_price, 2),
                "low_24h": round(venue.low_price, 2),
                "volume": venue.total_volume,
                "trades": venue.total_trades,
            })
        return tickers

    def allocate_order_id(self) -> int:
        """Globally sequence an order ID."""
        oid = self.order_id_counter
        self.order_id_counter += 1
        return oid
