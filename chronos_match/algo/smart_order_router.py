"""
ChronosMatch: Institutional Smart Order Router (SOR) Execution Engine.

Directs multi-venue order flow across fragmented markets using 4 institutional
routing algorithms:
1. NBBO Best Execution: Sweeps price-time priority across all exchanges.
2. Pro-Rata Depth Split: Allocates volume based on visible liquidity to minimize market impact.
3. Cost & Fee-Aware Router: Factors in venue maker-taker fees and rebates for lowest net cost.
4. Simultaneous Latency Sweep: Staggers order dispatch based on speed-of-light propagation
   to arrive concurrently at all exchange datacenters, defeating adverse quote fade.
"""

from dataclasses import dataclass, field
from enum import Enum
import math
import time
from typing import Dict, List, Optional, Tuple, Any

from chronos_match.market.sor_network import MultiVenueNetwork, ExchangeVenue, VenueFill
from chronos_match.market.nbbo import NBBOEngine, NBBORecord


class RoutingPolicy(str, Enum):
    """Institutional routing optimization policies."""
    NBBO_BEST_PRICE = "NBBO_BEST_PRICE"               # Standard Reg NMS Best Execution
    PRO_RATA_DEPTH = "PRO_RATA_DEPTH"                 # Split by visible order book depth
    COST_AND_FEE_OPTIMIZED = "COST_AND_FEE_OPTIMIZED" # Net price after exchange fees/rebates
    SIMULTANEOUS_SWEEP = "SIMULTANEOUS_SWEEP"         # Latency-staggered concurrent arrival


@dataclass
class SORChildOrder:
    """Individual child order dispatched to a specific exchange venue."""
    venue_id: str
    symbol: str
    side: str
    price: float
    quantity: int
    dispatch_delay_ms: float
    fills: List[VenueFill] = field(default_factory=list)
    remaining_qty: int = 0


@dataclass
class SORRoutingResult:
    """Consolidated outcome of an institutional multi-venue routed order."""
    parent_order_id: int
    symbol: str
    side: str
    requested_quantity: int
    filled_quantity: int
    average_fill_price: float
    total_notional: float
    total_fees_usd: float
    effective_all_in_price: float  # Price including fees: (notional + fees) / qty
    routing_policy: str
    execution_time_ms: float
    venue_allocations: Dict[str, int]
    child_orders: List[Dict[str, Any]]
    fills: List[Dict[str, Any]]
    price_improvement_usd: float    # Savings achieved vs routing blindly to single default venue

    def to_dict(self) -> Dict[str, Any]:
        return {
            "parent_order_id": self.parent_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "requested_quantity": self.requested_quantity,
            "filled_quantity": self.filled_quantity,
            "average_fill_price": round(self.average_fill_price, 2),
            "total_notional": round(self.total_notional, 2),
            "total_fees_usd": round(self.total_fees_usd, 4),
            "effective_all_in_price": round(self.effective_all_in_price, 4),
            "routing_policy": self.routing_policy,
            "execution_time_ms": round(self.execution_time_ms, 3),
            "venue_allocations": self.venue_allocations,
            "child_orders": self.child_orders,
            "fills": self.fills,
            "price_improvement_usd": round(self.price_improvement_usd, 2),
        }


class SmartOrderRouter:
    """
    Tier-1 Smart Order Router coordinating multi-exchange order execution.
    """

    def __init__(self, network: MultiVenueNetwork, nbbo_engine: Optional[NBBOEngine] = None):
        self.network = network
        self.nbbo_engine = nbbo_engine or NBBOEngine(network)
        self.parent_order_counter = 500_000
        self.execution_history: List[SORRoutingResult] = []

    def route_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        policy: Union[RoutingPolicy, str] = RoutingPolicy.NBBO_BEST_PRICE,
        limit_price: Optional[float] = None,
    ) -> SORRoutingResult:
        """
        Route an institutional parent order across the exchange network
        according to the selected optimization policy.
        """
        start_time = time.perf_counter()
        sym = symbol.upper()
        order_side = side.upper()
        p_policy = RoutingPolicy(policy) if isinstance(policy, str) else policy
        parent_id = self._next_parent_id()

        # Compute pre-trade NBBO state
        nbbo = self.nbbo_engine.compute_nbbo(sym)

        # Baseline single-venue benchmark price (e.g. NASDAQ only) for price improvement calculation
        default_tob = self.network.get_venue("NASDAQ").get_top_of_book(sym)
        single_venue_px = default_tob["best_ask"] if order_side == "BUY" else default_tob["best_bid"]
        if single_venue_px <= 0:
            single_venue_px = nbbo.national_best_offer if order_side == "BUY" else nbbo.national_best_bid

        # Dispatch according to policy
        if p_policy == RoutingPolicy.NBBO_BEST_PRICE:
            child_orders, fills = self._execute_nbbo_sweep(sym, order_side, quantity, nbbo, limit_price)
        elif p_policy == RoutingPolicy.PRO_RATA_DEPTH:
            child_orders, fills = self._execute_pro_rata_split(sym, order_side, quantity, nbbo, limit_price)
        elif p_policy == RoutingPolicy.COST_AND_FEE_OPTIMIZED:
            child_orders, fills = self._execute_cost_optimized(sym, order_side, quantity, nbbo, limit_price)
        elif p_policy == RoutingPolicy.SIMULTANEOUS_SWEEP:
            child_orders, fills = self._execute_simultaneous_latency_sweep(sym, order_side, quantity, nbbo, limit_price)
        else:
            child_orders, fills = self._execute_nbbo_sweep(sym, order_side, quantity, nbbo, limit_price)

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        # Aggregate outcomes
        total_qty = sum(f.quantity for f in fills)
        total_notional = sum(f.notional for f in fills)
        total_fees = sum(f.fee_usd for f in fills)
        avg_px = (total_notional / total_qty) if total_qty > 0 else 0.0

        # Effective price: buyers pay price + fee; sellers receive price - fee
        if order_side == "BUY":
            eff_price = ((total_notional + total_fees) / total_qty) if total_qty > 0 else 0.0
            price_improvement = (single_venue_px * total_qty) - total_notional
        else:
            eff_price = ((total_notional - total_fees) / total_qty) if total_qty > 0 else 0.0
            price_improvement = total_notional - (single_venue_px * total_qty)

        venue_allocations: Dict[str, int] = {}
        for f in fills:
            venue_allocations[f.venue_id] = venue_allocations.get(f.venue_id, 0) + f.quantity

        child_dicts = []
        for c in child_orders:
            child_dicts.append({
                "venue_id": c.venue_id,
                "quantity": c.quantity,
                "price": c.price,
                "dispatch_delay_ms": round(c.dispatch_delay_ms, 3),
                "filled_qty": sum(f.quantity for f in c.fills),
                "remaining_qty": c.remaining_qty,
            })

        fill_dicts = [
            {
                "venue_id": f.venue_id,
                "side": f.side,
                "price": f.price,
                "quantity": f.quantity,
                "notional": f.notional,
                "fee_usd": f.fee_usd,
                "time_ms": f.execution_time_ms,
            }
            for f in fills
        ]

        result = SORRoutingResult(
            parent_order_id=parent_id,
            symbol=sym,
            side=order_side,
            requested_quantity=quantity,
            filled_quantity=total_qty,
            average_fill_price=avg_px,
            total_notional=total_notional,
            total_fees_usd=total_fees,
            effective_all_in_price=eff_price,
            routing_policy=p_policy.value,
            execution_time_ms=elapsed_ms,
            venue_allocations=venue_allocations,
            child_orders=child_dicts,
            fills=fill_dicts,
            price_improvement_usd=price_improvement,
        )

        self.execution_history.append(result)
        if len(self.execution_history) > 200:
            self.execution_history.pop(0)

        return result

    # ── Policy Implementations ─────────────────────────────────────────────

    def _execute_nbbo_sweep(
        self,
        symbol: str,
        side: str,
        quantity: int,
        nbbo: NBBORecord,
        limit_price: Optional[float],
    ) -> Tuple[List[SORChildOrder], List[VenueFill]]:
        """Sweep venues in price-time priority starting with best quote."""
        remaining_qty = quantity
        all_fills: List[VenueFill] = []
        child_orders: List[SORChildOrder] = []

        # Sort candidate venues by price advantage
        venue_items = list(nbbo.venue_quotes.items())
        if side == "BUY":
            # Lowest ask first
            venue_items.sort(key=lambda x: (x[1]["best_ask"] if x[1]["best_ask"] > 0 else 999999.0, x[1]["base_latency_ms"]))
        else:
            # Highest bid first
            venue_items.sort(key=lambda x: (-x[1]["best_bid"] if x[1]["best_bid"] > 0 else 999999.0, x[1]["base_latency_ms"]))

        for vid, tob in venue_items:
            if remaining_qty <= 0:
                break
            target_px = tob["best_ask"] if side == "BUY" else tob["best_bid"]
            avail_size = tob["ask_size"] if side == "BUY" else tob["bid_size"]

            if target_px <= 0 or avail_size <= 0:
                continue
            if limit_price is not None:
                if side == "BUY" and target_px > limit_price:
                    continue
                if side == "SELL" and target_px < limit_price:
                    continue

            # Sweep available volume at this price
            slice_qty = min(remaining_qty, avail_size)
            venue = self.network.get_venue(vid)
            fills, rem = venue.execute_limit_order(symbol, side, target_px, slice_qty)

            child = SORChildOrder(
                venue_id=vid,
                symbol=symbol,
                side=side,
                price=target_px,
                quantity=slice_qty,
                dispatch_delay_ms=0.0,
                fills=fills,
                remaining_qty=rem,
            )
            child_orders.append(child)
            all_fills.extend(fills)
            filled_slice = sum(f.quantity for f in fills)
            remaining_qty -= filled_slice

        # If remaining quantity exists and not strictly limited, sweep remaining depth
        if remaining_qty > 0 and limit_price is None:
            # Fall back to largest liquidity venue for balance
            for vid, _ in venue_items:
                if remaining_qty <= 0:
                    break
                venue = self.network.get_venue(vid)
                fills = venue.execute_market_order(symbol, side, remaining_qty)
                if fills:
                    filled_now = sum(f.quantity for f in fills)
                    remaining_qty -= filled_now
                    all_fills.extend(fills)

        return child_orders, all_fills

    def _execute_pro_rata_split(
        self,
        symbol: str,
        side: str,
        quantity: int,
        nbbo: NBBORecord,
        limit_price: Optional[float],
    ) -> Tuple[List[SORChildOrder], List[VenueFill]]:
        """Split parent order across venues proportionally according to visible depth."""
        target_venues: List[Tuple[str, float, int]] = []
        total_visible_size = 0

        best_px = nbbo.national_best_offer if side == "BUY" else nbbo.national_best_bid
        for vid, tob in nbbo.venue_quotes.items():
            px = tob["best_ask"] if side == "BUY" else tob["best_bid"]
            size = tob["ask_size"] if side == "BUY" else tob["bid_size"]
            # Check within 1-2 ticks of best price
            if px > 0 and size > 0 and abs(px - best_px) <= 0.03:
                target_venues.append((vid, px, size))
                total_visible_size += size

        if total_visible_size == 0:
            return self._execute_nbbo_sweep(symbol, side, quantity, nbbo, limit_price)

        child_orders: List[SORChildOrder] = []
        all_fills: List[VenueFill] = []
        allocated_so_far = 0

        for i, (vid, px, size) in enumerate(target_venues):
            if i == len(target_venues) - 1:
                child_qty = quantity - allocated_so_far
            else:
                ratio = size / total_visible_size
                child_qty = max(1, int(round(quantity * ratio)))
                allocated_so_far += child_qty

            if child_qty <= 0:
                continue

            venue = self.network.get_venue(vid)
            fills, rem = venue.execute_limit_order(symbol, side, px, child_qty)
            child = SORChildOrder(
                venue_id=vid,
                symbol=symbol,
                side=side,
                price=px,
                quantity=child_qty,
                dispatch_delay_ms=0.0,
                fills=fills,
                remaining_qty=rem,
            )
            child_orders.append(child)
            all_fills.extend(fills)

        return child_orders, all_fills

    def _execute_cost_optimized(
        self,
        symbol: str,
        side: str,
        quantity: int,
        nbbo: NBBORecord,
        limit_price: Optional[float],
    ) -> Tuple[List[SORChildOrder], List[VenueFill]]:
        """
        Rank venues by all-in effective cost:
        For Buyer: Effective Cost = Price * (1 + Taker Fee bps / 10000)
        For Seller: Effective Proceeds = Price * (1 - Taker Fee bps / 10000)
        """
        candidates: List[Tuple[float, str, float, int]] = []
        for vid, tob in nbbo.venue_quotes.items():
            px = tob["best_ask"] if side == "BUY" else tob["best_bid"]
            size = tob["ask_size"] if side == "BUY" else tob["bid_size"]
            taker_fee_bps = tob["taker_fee_bps"]
            if px > 0 and size > 0:
                if side == "BUY":
                    # Lower all-in cost is better
                    all_in_cost = px * (1.0 + (taker_fee_bps / 10000.0))
                    candidates.append((all_in_cost, vid, px, size))
                else:
                    # Higher all-in proceeds is better (negate for ascending sort)
                    all_in_proceeds = px * (1.0 - (taker_fee_bps / 10000.0))
                    candidates.append((-all_in_proceeds, vid, px, size))

        candidates.sort(key=lambda x: x[0])

        remaining_qty = quantity
        child_orders: List[SORChildOrder] = []
        all_fills: List[VenueFill] = []

        for _, vid, px, size in candidates:
            if remaining_qty <= 0:
                break
            slice_qty = min(remaining_qty, size)
            venue = self.network.get_venue(vid)
            fills, rem = venue.execute_limit_order(symbol, side, px, slice_qty)

            child = SORChildOrder(
                venue_id=vid,
                symbol=symbol,
                side=side,
                price=px,
                quantity=slice_qty,
                dispatch_delay_ms=0.0,
                fills=fills,
                remaining_qty=rem,
            )
            child_orders.append(child)
            all_fills.extend(fills)
            filled_slice = sum(f.quantity for f in fills)
            remaining_qty -= filled_slice

        # Fill remaining balance if needed
        if remaining_qty > 0 and candidates:
            best_vid = candidates[0][1]
            fills = self.network.get_venue(best_vid).execute_market_order(symbol, side, remaining_qty)
            all_fills.extend(fills)

        return child_orders, all_fills

    def _execute_simultaneous_latency_sweep(
        self,
        symbol: str,
        side: str,
        quantity: int,
        nbbo: NBBORecord,
        limit_price: Optional[float],
    ) -> Tuple[List[SORChildOrder], List[VenueFill]]:
        """
        Calculates propagation delay to each datacenter and schedules staggered
        order dispatch times (t_depart = max_latency - latency_to_venue) so that
        all child slices hit exchange matching engines at the EXACT SAME microsecond!
        Prevents HFT market makers on distant exchanges from seeing fills and pulling quotes.
        """
        # Determine candidate venues with top quotes
        best_px = nbbo.national_best_offer if side == "BUY" else nbbo.national_best_bid
        active_venues = [
            (vid, tob["best_ask"] if side == "BUY" else tob["best_bid"], tob["ask_size"] if side == "BUY" else tob["bid_size"])
            for vid, tob in nbbo.venue_quotes.items()
            if (tob["best_ask"] if side == "BUY" else tob["best_bid"]) > 0
        ]

        if not active_venues:
            return self._execute_nbbo_sweep(symbol, side, quantity, nbbo, limit_price)

        # Compute total latency to each exchange from our SOR router (located at Carteret/NASDAQ)
        latencies: Dict[str, float] = {}
        for vid, _, _ in active_venues:
            venue = self.network.get_venue(vid)
            lat = self.network.get_pairwise_latency_ms("NASDAQ", vid) + venue.config.base_latency_ms + venue.config.speed_bump_ms
            latencies[vid] = lat

        max_latency = max(latencies.values())

        # Proportional slice allocation
        total_size = sum(v[2] for v in active_venues) or 1
        child_orders: List[SORChildOrder] = []
        all_fills: List[VenueFill] = []
        allocated = 0

        for i, (vid, px, size) in enumerate(active_venues):
            if i == len(active_venues) - 1:
                slice_qty = quantity - allocated
            else:
                slice_qty = max(1, int(round(quantity * (size / total_size))))
                allocated += slice_qty

            if slice_qty <= 0:
                continue

            # Staggered dispatch delay: highest latency venue dispatches first (delay 0.0),
            # lower latency venues wait (delay = max_latency - this_latency)
            stagger_delay_ms = max(0.0, max_latency - latencies[vid])

            venue = self.network.get_venue(vid)
            fills, rem = venue.execute_limit_order(symbol, side, px, slice_qty)
            child = SORChildOrder(
                venue_id=vid,
                symbol=symbol,
                side=side,
                price=px,
                quantity=slice_qty,
                dispatch_delay_ms=stagger_delay_ms,
                fills=fills,
                remaining_qty=rem,
            )
            child_orders.append(child)
            all_fills.extend(fills)

        return child_orders, all_fills

    def _next_parent_id(self) -> int:
        pid = self.parent_order_counter
        self.parent_order_counter += 1
        return pid
