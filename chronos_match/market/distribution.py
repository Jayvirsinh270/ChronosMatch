"""
ChronosMatch: Financial Order Distribution & Stochastic Price Generator.
Simulates realistic exchange order flow:
- 60% Limit Orders (Clustered around midpoint).
- 25% Cancellations (Targeting resting orders).
- 15% Aggressive Market Sweeps (Whale orders crossing the spread).
"""

import random
import time
from typing import Optional, Set
from collections import deque
from chronos_match.protocol import (
    OrderPacket,
    OrderSide,
    OrderType,
    to_fixed_price,
    PRICE_SCALE,
)

class OrderDistributionGenerator:
    """
    Stochastic generator modeling realistic financial order flow.
    Maintains a rolling active order pool so Cancellations reference real resting orders.
    """
    def __init__(
        self,
        symbol_id: int = 1,
        initial_price: float = 150.00,
        tick_size: float = 0.01,
        limit_ratio: float = 0.60,
        cancel_ratio: float = 0.25,
        market_ratio: float = 0.15,
        seed: Optional[int] = None
    ):
        self.symbol_id = symbol_id
        self.midpoint_price = to_fixed_price(initial_price)
        self.tick_size_int = to_fixed_price(tick_size)

        self.limit_ratio = limit_ratio
        self.cancel_ratio = cancel_ratio
        self.market_ratio = market_ratio

        if seed is not None:
            random.seed(seed)

        self._next_order_id = 1
        self._active_order_ids = deque(maxlen=10_000)

    def generate_next_order(self, ingress_timestamp_ns: Optional[int] = None) -> OrderPacket:
        """
        Generate the next synthetic financial order packet.
        """
        if ingress_timestamp_ns is None:
            ingress_timestamp_ns = time.perf_counter_ns()

        # Random walk on midpoint price (1 tick up, down, or stay)
        drift = random.choice([-self.tick_size_int, 0, self.tick_size_int])
        self.midpoint_price = max(self.tick_size_int * 10, self.midpoint_price + drift)

        r = random.random()

        # 1. CANCELLATION (25%) - requires active resting orders
        if r < self.cancel_ratio and len(self._active_order_ids) > 10:
            target_order_id = self._active_order_ids.popleft()
            return OrderPacket(
                order_id=target_order_id,
                client_id=1,
                price=0,
                quantity=0,
                side=0,
                order_type=int(OrderType.CANCEL),
                symbol_id=self.symbol_id,
                timestamp_in_ns=ingress_timestamp_ns
            )

        # 2. AGGRESSIVE MARKET SWEEP / WHALE ORDER (15%)
        elif r < (self.cancel_ratio + self.market_ratio):
            order_id = self._next_order_id
            self._next_order_id += 1

            side = random.choice([int(OrderSide.BUY), int(OrderSide.SELL)])
            # 20% chance of a massive Whale Sweep
            is_whale = random.random() < 0.20
            quantity = random.randint(1000, 5000) if is_whale else random.randint(50, 200)

            # Cross the spread aggressively (market sweep)
            offset = random.randint(1, 10) * self.tick_size_int
            price = self.midpoint_price + offset if side == OrderSide.BUY else max(self.tick_size_int, self.midpoint_price - offset)

            return OrderPacket(
                order_id=order_id,
                client_id=random.randint(100, 999),
                price=price,
                quantity=quantity,
                side=side,
                order_type=int(OrderType.LIMIT), # Aggressive Limit acting as Market Sweep
                symbol_id=self.symbol_id,
                timestamp_in_ns=ingress_timestamp_ns
            )

        # 3. PASSIVE LIMIT ORDER (60%)
        else:
            order_id = self._next_order_id
            self._next_order_id += 1
            self._active_order_ids.append(order_id)

            side = random.choice([int(OrderSide.BUY), int(OrderSide.SELL)])
            # Distance from midpoint (1 to 25 ticks away from mid)
            ticks_away = random.randint(1, 25) * self.tick_size_int
            price = self.midpoint_price - ticks_away if side == OrderSide.BUY else self.midpoint_price + ticks_away

            quantity = random.choice([10, 25, 50, 100, 200, 500])

            return OrderPacket(
                order_id=order_id,
                client_id=random.randint(1, 50),
                price=price,
                quantity=quantity,
                side=side,
                order_type=int(OrderType.LIMIT),
                symbol_id=self.symbol_id,
                timestamp_in_ns=ingress_timestamp_ns
            )

MarketDistribution = OrderDistributionGenerator
