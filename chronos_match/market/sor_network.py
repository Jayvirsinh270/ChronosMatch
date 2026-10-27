"""
ChronosMatch: Multi-Venue Exchange Network Simulation.

Simulates geographically distributed institutional exchange venues (e.g., NASDAQ, NYSE,
BATS, IEX) with realistic fiber/microwave latencies, maker-taker fee schedules,
and independent order books with microstructural price jitter.
"""

from dataclasses import dataclass, field
import math
import random
import time
from typing import Dict, List, Optional, Tuple, Any

from chronos_match.protocol import (
    OrderSide,
    OrderType,
    to_fixed_price,
    from_fixed_price,
    TradePacket,
)
from chronos_match.engine.order_book_cy import FastOrderBook


@dataclass
class VenueConfig:
    """Institutional exchange configuration and fee schedule."""
    venue_id: str             # e.g., "NASDAQ", "NYSE", "BATS", "IEX"
    name: str                 # Full name
    mic_code: str             # Market Identifier Code (e.g., "XNAS", "XNYS", "BATS", "IEXG")
    location: str             # Datacenter location (e.g., "Carteret, NJ (NY4)", "Mahwah, NJ")
    taker_fee_bps: float      # Taker fee in basis points (e.g., 30.0 bps = 0.30%)
    maker_rebate_bps: float   # Maker rebate in basis points (e.g., 20.0 bps = 0.20%)
    base_latency_ms: float    # Internal matching engine processing latency
    speed_bump_ms: float = 0.0 # Intentional delay coil (e.g., 0.35 ms on IEX)


@dataclass
class VenueFill:
    """Record of an execution fill on a specific exchange venue."""
    venue_id: str
    order_id: int
    symbol: str
    side: str
    price: float
    quantity: int
    notional: float
    fee_usd: float           # Net fee paid (or rebate earned if negative)
    execution_time_ms: float
    is_maker: bool = False


class ExchangeVenue:
    """
    Simulated independent exchange venue with its own Level 2 matching book,
    fee schedule, and geographic latency characteristics.
    """

    def __init__(self, config: VenueConfig):
        self.config = config
        self.venue_id = config.venue_id
        # Independent order books keyed by symbol (e.g. "NVDA", "AAPL")
        self.books: Dict[str, FastOrderBook] = {}
        self.order_counter: int = 1_000_000 + abs(hash(config.venue_id)) % 800_000
        self.total_volume: int = 0
        self.total_trades: int = 0
        self.total_fees_collected: float = 0.0

    def get_or_create_book(self, symbol: str) -> FastOrderBook:
        """Get or initialize isolated order book for this instrument on this exchange."""
        sym = symbol.upper()
        if sym not in self.books:
            self.books[sym] = FastOrderBook(arena_capacity=50_000)
        return self.books[sym]

    def seed_liquidity(self, symbol: str, mid_price: float, spread_bps: float = 5.0, depth_levels: int = 5) -> None:
        """Seed initial two-sided resting liquidity with micro-venue price variations."""
        book = self.get_or_create_book(symbol)
        half_spread = (mid_price * (spread_bps / 10000.0)) / 2.0
        tick_size = 0.01

        # Add venue-specific micro-bias (-1 to +1 tick)
        bias = random.choice([-0.01, 0.0, 0.01])
        base_bid = math.floor((mid_price - half_spread + bias) / tick_size) * tick_size
        base_ask = math.ceil((mid_price + half_spread + bias) / tick_size) * tick_size
        if base_ask <= base_bid:
            base_ask = base_bid + tick_size

        for i in range(depth_levels):
            bid_p = round(base_bid - (i * tick_size), 2)
            ask_p = round(base_ask + (i * tick_size), 2)
            size = random.randint(200, 1500)

            # Insert resting Buy (side 0 = BUY)
            book.add_limit_order(
                order_id=self._next_order_id(),
                client_id=100,
                price=to_fixed_price(bid_p),
                quantity=size,
                side=0,
            )
            # Insert resting Sell (side 1 = SELL)
            book.add_limit_order(
                order_id=self._next_order_id(),
                client_id=101,
                price=to_fixed_price(ask_p),
                quantity=size,
                side=1,
            )

    def execute_market_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        client_id: int = 999,
    ) -> List[VenueFill]:
        """
        Execute aggressive order against this venue's order book,
        calculating realistic taker fees and fills.
        """
        book = self.get_or_create_book(symbol)
        is_buy = (side.upper() == "BUY")
        side_val = 0 if is_buy else 1
        # Deep crossing price ensures aggressive immediate execution
        cross_px = to_fixed_price(1_000_000.0 if is_buy else 0.01)
        oid = self._next_order_id()

        start_time = time.perf_counter()
        matches = book.add_limit_order(
            order_id=oid,
            client_id=client_id,
            price=cross_px,
            quantity=quantity,
            side=side_val,
        )
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0 + self.config.base_latency_ms + self.config.speed_bump_ms

        fills: List[VenueFill] = []
        for m in (matches or []):
            exec_px_fixed = m.get("execution_price", 0)
            price = from_fixed_price(exec_px_fixed)
            qty = m.get("quantity", 0)
            notional = price * qty
            fee_usd = notional * (self.config.taker_fee_bps / 10000.0)

            self.total_volume += qty
            self.total_trades += 1
            self.total_fees_collected += fee_usd

            fills.append(VenueFill(
                venue_id=self.venue_id,
                order_id=oid,
                symbol=symbol,
                side=side.upper(),
                price=price,
                quantity=qty,
                notional=notional,
                fee_usd=round(fee_usd, 4),
                execution_time_ms=round(elapsed_ms, 3),
                is_maker=False,
            ))
        return fills

    def execute_limit_order(
        self,
        symbol: str,
        side: str,
        price: float,
        quantity: int,
        client_id: int = 999,
    ) -> Tuple[List[VenueFill], int]:
        """
        Execute limit order against this venue's book.
        Returns matched fills and any remaining resting quantity.
        """
        book = self.get_or_create_book(symbol)
        side_val = 0 if side.upper() == "BUY" else 1
        oid = self._next_order_id()

        start_time = time.perf_counter()
        matches = book.add_limit_order(
            order_id=oid,
            client_id=client_id,
            price=to_fixed_price(price),
            quantity=quantity,
            side=side_val,
        )
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0 + self.config.base_latency_ms + self.config.speed_bump_ms

        fills: List[VenueFill] = []
        filled_qty = 0
        for m in (matches or []):
            exec_px_fixed = m.get("execution_price", 0)
            trade_px = from_fixed_price(exec_px_fixed)
            qty = m.get("quantity", 0)
            filled_qty += qty
            notional = trade_px * qty
            fee_usd = notional * (self.config.taker_fee_bps / 10000.0)

            self.total_volume += qty
            self.total_trades += 1
            self.total_fees_collected += fee_usd

            fills.append(VenueFill(
                venue_id=self.venue_id,
                order_id=oid,
                symbol=symbol,
                side=side.upper(),
                price=trade_px,
                quantity=qty,
                notional=notional,
                fee_usd=round(fee_usd, 4),
                execution_time_ms=round(elapsed_ms, 3),
                is_maker=False,
            ))

        remaining = max(0, quantity - filled_qty)
        return fills, remaining

    def get_top_of_book(self, symbol: str) -> Dict[str, Any]:
        """Return Top-of-Book Best Bid & Best Offer (BBO) with volume and queue order count."""
        book = self.get_or_create_book(symbol)
        bb_fixed = book.best_bid_price
        ba_fixed = book.best_ask_price

        bb = from_fixed_price(bb_fixed) if bb_fixed > 0 else 0.0
        ba = from_fixed_price(ba_fixed) if ba_fixed > 0 else 0.0

        bid_vol = book.best_bid_volume if bb_fixed > 0 else 0
        ask_vol = book.best_ask_volume if ba_fixed > 0 else 0

        spread = round(ba - bb, 2) if (ba > 0 and bb > 0) else 0.0
        spread_bps = round((spread / bb) * 10000.0, 1) if bb > 0 else 0.0

        return {
            "venue_id": self.venue_id,
            "name": self.config.name,
            "mic": self.config.mic_code,
            "location": self.config.location,
            "taker_fee_bps": self.config.taker_fee_bps,
            "maker_rebate_bps": self.config.maker_rebate_bps,
            "base_latency_ms": self.config.base_latency_ms,
            "speed_bump_ms": self.config.speed_bump_ms,
            "best_bid": bb,
            "bid_size": bid_vol,
            "best_ask": ba,
            "ask_size": ask_vol,
            "spread": spread,
            "spread_bps": spread_bps,
        }

    def get_depth_ladder(self, symbol: str, levels: int = 5) -> Dict[str, List[Dict[str, Any]]]:
        """Return multi-level depth ladder for this exchange."""
        book = self.get_or_create_book(symbol)
        l2 = book.get_l2_snapshot(max_levels=levels)
        bids = [{"price": from_fixed_price(b["price"]), "size": b["volume"]} for b in l2.get("bids", [])]
        asks = [{"price": from_fixed_price(a["price"]), "size": a["volume"]} for a in l2.get("asks", [])]
        return {"bids": bids, "asks": asks}

    def _next_order_id(self) -> int:
        oid = self.order_counter
        self.order_counter += 1
        return oid


class MultiVenueNetwork:
    """
    Interconnected multi-venue network simulating major financial centers:
    NASDAQ (Carteret, NJ), NYSE (Mahwah, NJ), BATS (Secaucus, NJ), and IEX (Weehawken, NJ with speed bump).
    Also supports Crypto exchanges (Binance TY3, Coinbase Virginia, Bybit SG1).
    """

    DEFAULT_EQUITIES_VENUES = [
        VenueConfig(
            venue_id="NASDAQ",
            name="Nasdaq Stock Market",
            mic_code="XNAS",
            location="Carteret, NJ (NY4)",
            taker_fee_bps=3.0,
            maker_rebate_bps=2.0,
            base_latency_ms=0.35,
            speed_bump_ms=0.0,
        ),
        VenueConfig(
            venue_id="NYSE",
            name="New York Stock Exchange",
            mic_code="XNYS",
            location="Mahwah, NJ",
            taker_fee_bps=2.8,
            maker_rebate_bps=1.8,
            base_latency_ms=0.42,
            speed_bump_ms=0.0,
        ),
        VenueConfig(
            venue_id="BATS",
            name="Cboe BZX Exchange",
            mic_code="BATS",
            location="Secaucus, NJ (NY4)",
            taker_fee_bps=2.5,
            maker_rebate_bps=2.2,
            base_latency_ms=0.30,
            speed_bump_ms=0.0,
        ),
        VenueConfig(
            venue_id="IEX",
            name="Investors Exchange",
            mic_code="IEXG",
            location="Weehawken, NJ (Speed Bump 350µs)",
            taker_fee_bps=0.9,
            maker_rebate_bps=0.0,
            base_latency_ms=0.38,
            speed_bump_ms=0.35,  # 350 microsecond discrete speed bump
        ),
    ]

    # Inter-datacenter microwave/fiber propagation latency matrix (in milliseconds)
    LATENCY_MATRIX_MS: Dict[Tuple[str, str], float] = {
        ("NASDAQ", "NYSE"): 0.38,
        ("NYSE", "NASDAQ"): 0.38,
        ("NASDAQ", "BATS"): 0.12,
        ("BATS", "NASDAQ"): 0.12,
        ("NASDAQ", "IEX"): 0.45,
        ("IEX", "NASDAQ"): 0.45,
        ("NYSE", "BATS"): 0.35,
        ("BATS", "NYSE"): 0.35,
        ("NYSE", "IEX"): 0.42,
        ("IEX", "NYSE"): 0.42,
        ("BATS", "IEX"): 0.36,
        ("IEX", "BATS"): 0.36,
    }

    def __init__(self, venue_configs: Optional[List[VenueConfig]] = None):
        configs = venue_configs or self.DEFAULT_EQUITIES_VENUES
        self.venues: Dict[str, ExchangeVenue] = {c.venue_id: ExchangeVenue(c) for c in configs}

    def get_venue(self, venue_id: str) -> ExchangeVenue:
        vid = venue_id.upper()
        if vid not in self.venues:
            raise KeyError(f"Unknown venue: {venue_id}. Available: {list(self.venues.keys())}")
        return self.venues[vid]

    def list_venues(self) -> List[Dict[str, Any]]:
        """Return catalog of active exchange venues and their execution attributes."""
        res = []
        for v in self.venues.values():
            res.append({
                "venue_id": v.venue_id,
                "name": v.config.name,
                "mic": v.config.mic_code,
                "location": v.config.location,
                "taker_fee_bps": v.config.taker_fee_bps,
                "maker_rebate_bps": v.config.maker_rebate_bps,
                "base_latency_ms": v.config.base_latency_ms,
                "speed_bump_ms": v.config.speed_bump_ms,
                "total_volume": v.total_volume,
                "total_trades": v.total_trades,
            })
        return res

    def get_pairwise_latency_ms(self, from_venue: str, to_venue: str) -> float:
        """Get speed-of-light propagation latency between two exchange datacenters."""
        v1 = from_venue.upper()
        v2 = to_venue.upper()
        if v1 == v2:
            return 0.05
        return self.LATENCY_MATRIX_MS.get((v1, v2), 0.40)

    def seed_network(self, symbol: str, mid_price: float, spread_bps: float = 6.0) -> None:
        """Seed all participating exchange venues with independent realistic liquidity."""
        for venue in self.venues.values():
            # Apply subtle randomized spread variation per exchange
            venue_spread = spread_bps * random.uniform(0.85, 1.25)
            venue.seed_liquidity(symbol=symbol, mid_price=mid_price, spread_bps=venue_spread, depth_levels=6)

    def inject_market_shock(self, symbol: str, venue_id: str, price_delta: float) -> None:
        """
        Simulate an asynchronous order arrival shock on one venue,
        creating an instantaneous arbitrage window across exchanges!
        """
        venue = self.get_venue(venue_id)
        book = venue.get_or_create_book(symbol)
        bb = from_fixed_price(book.best_bid_price)
        ba = from_fixed_price(book.best_ask_price)

        delta = abs(price_delta) if abs(price_delta) > 0.05 else 0.40
        if price_delta >= 0:
            # Aggressive institutional buyer submits high bid crossing over other venues' asks
            target_px = round((ba if ba > 0 else 130.0) + delta, 2)
            venue.execute_limit_order(symbol, side="BUY", price=target_px, quantity=10_000)
        else:
            # Aggressive institutional seller submits low ask crossing under other venues' bids
            target_px = round(max(0.01, (bb if bb > 0 else 130.0) - delta), 2)
            venue.execute_limit_order(symbol, side="SELL", price=target_px, quantity=10_000)
