"""
ChronosMatch: Machine Learning & Microstructure Market Impact Engine.

Implements an adaptive online learning model that adjusts market pricing,
spread width, and order book depth based on live buying and selling pressure.
Combines real macroeconomic historical price paths with endogenous order flow impact.

Theoretical Foundation:
- Almgren-Chriss (2000): Optimal Execution of Portfolio Transactions
- Kyle (1985): Continuous Auctions and Informed Trader Price Impact (Kyle's Lambda)
- Online Stochastic Gradient / EWMA Adaptive Learning of Liquidity Sensitivity
"""

from dataclasses import dataclass, field
import math
import time
from typing import Dict, List, Optional, Tuple, Any


@dataclass
class MarketImpactState:
    """Real-time telemetry of the adaptive ML impact model."""
    baseline_price: float
    effective_mid: float
    net_order_flow: int          # Positive = net buying, Negative = net selling
    order_flow_imbalance: float  # -1.0 (all sells) to +1.0 (all buys)
    kyle_lambda: float           # Price sensitivity ($/share impact coefficient)
    cumulative_impact_pct: float
    spread_multiplier: float     # > 1.0 when liquidity is depleted by large orders
    buy_pressure: float
    sell_pressure: float


class MLMarketImpactEngine:
    """
    Adaptive Microstructure Model bridging real historical stock data with live order flow.
    Dynamically moves the market up on aggressive buys and down on aggressive sells.
    """

    def __init__(
        self,
        base_price: float = 150.00,
        daily_volatility: float = 0.02,
        avg_daily_volume: int = 1_000_000,
        decay_half_life_sec: float = 8.0,
        learning_rate: float = 0.05,
    ):
        self.base_price = base_price
        self.daily_volatility = daily_volatility
        self.avg_daily_volume = max(10_000, avg_daily_volume)
        self.learning_rate = learning_rate

        # Decay rate kappa for transient price impact: kappa = ln(2) / half_life
        self.kappa = math.log(2.0) / max(0.5, decay_half_life_sec)

        # Baseline Kyle's Lambda: lambda ~ sigma / sqrt(DailyVolume)
        # Scaled so 10,000 shares moves the price by ~0.10% - 0.50% depending on volatility
        self.base_lambda = (daily_volatility / math.sqrt(self.avg_daily_volume)) * 25.0
        self.current_lambda = self.base_lambda

        # State tracking
        self.last_update_time = time.time()
        self.transient_impact_shares = 0.0
        self.permanent_impact_offset = 0.0
        self.transient_spread_elevation = 0.0

        # Rolling Order Flow Imbalance (OFI)
        self.rolling_buy_vol = 0
        self.rolling_sell_vol = 0
        self.ofi = 0.0  # -1.0 to +1.0

        # History for performance and visualization
        self.impact_history: List[Dict[str, Any]] = []

    def set_baseline_price(self, price: float) -> None:
        """Update macro historical anchor price (from real stock dataset)."""
        if price > 0:
            self.base_price = price

    def record_execution(self, side: int, quantity: int, execution_price: float, is_taker: bool = True) -> float:
        """
        Feed an executed trade into the ML impact model.
        Returns the instantaneous price impact delta ($).
        """
        now = time.time()
        self._apply_time_decay(now)

        # Side: 0 = BUY (Aggressor bought -> upward pressure), 1 = SELL (Aggressor sold -> downward pressure)
        signed_shares = quantity if side == 0 else -quantity

        if side == 0:
            self.rolling_buy_vol += quantity
        else:
            self.rolling_sell_vol += quantity

        # Update Order Flow Imbalance
        tot = self.rolling_buy_vol + self.rolling_sell_vol
        if tot > 0:
            self.ofi = (self.rolling_buy_vol - self.rolling_sell_vol) / tot

        # 1. Permanent Price Impact (Almgren-Chriss linear impact)
        # Permanent drift that does not decay
        impact_pct = self.current_lambda * signed_shares
        # Clamp single trade impact to max 3.0% to prevent unbounded single-trade shocks
        impact_pct = max(-0.03, min(0.03, impact_pct))
        delta_p = self.base_price * impact_pct

        # 20% of impact is permanent; 80% is transient (recovers as liquidity replenishes)
        self.permanent_impact_offset += delta_p * 0.20
        self.transient_impact_shares += signed_shares * 0.80

        # 2. Transient Spread Widening
        # Large aggressive orders temporarily deplete liquidity on that side of the book
        fraction_of_depth = min(1.0, quantity / (self.avg_daily_volume * 0.005 + 100))
        self.transient_spread_elevation = min(2.5, self.transient_spread_elevation + fraction_of_depth * 1.5)

        # 3. Online ML Parameter Adaptation:
        # If trading volume exceeds expectation, increase lambda (market is illiquid/reactive)
        # If trading volume is low, relax lambda back toward baseline
        expected_tick_vol = self.avg_daily_volume / 20_000.0  # expected shares per event
        volume_surprise = (quantity - expected_tick_vol) / max(1.0, expected_tick_vol)
        grad = self.learning_rate * min(1.0, max(-0.5, volume_surprise))
        self.current_lambda = max(self.base_lambda * 0.2, min(self.base_lambda * 5.0, self.current_lambda * (1.0 + grad * 0.05)))

        self.last_update_time = now
        return delta_p

    def _apply_time_decay(self, now: Optional[float] = None) -> None:
        """Exponential decay of transient impact and spread elevation over elapsed time."""
        current_t = now if now is not None else time.time()
        dt = current_t - self.last_update_time
        if dt > 0:
            decay_factor = math.exp(-self.kappa * dt)
            self.transient_impact_shares *= decay_factor
            self.transient_spread_elevation *= decay_factor

            # Slow decay of rolling volume memory (half-life ~ 15 seconds)
            vol_decay = math.exp(-0.046 * dt)
            self.rolling_buy_vol = int(self.rolling_buy_vol * vol_decay)
            self.rolling_sell_vol = int(self.rolling_sell_vol * vol_decay)

            self.last_update_time = current_t

    def get_effective_price(self) -> float:
        """
        Calculate current effective fair market price:
        Effective = BasePrice + PermanentImpact + (CurrentLambda * TransientShares * BasePrice)
        """
        self._apply_time_decay()
        transient_p = self.base_price * (self.current_lambda * self.transient_impact_shares)
        effective = self.base_price + self.permanent_impact_offset + transient_p
        return max(0.01, round(effective, 2))

    def get_spread_multiplier(self) -> float:
        """
        Multiplier applied to the base bid-ask spread.
        Expands when aggressive orders consume book depth, then exponentially relaxes.
        """
        self._apply_time_decay()
        return round(1.0 + self.transient_spread_elevation, 3)

    def get_telemetry(self) -> MarketImpactState:
        """Return structured telemetry for monitoring and web terminal dashboard."""
        self._apply_time_decay()
        eff = self.get_effective_price()
        cum_impact = ((eff - self.base_price) / max(0.01, self.base_price)) * 100.0
        tot_vol = max(1, self.rolling_buy_vol + self.rolling_sell_vol)

        return MarketImpactState(
            baseline_price=round(self.base_price, 2),
            effective_mid=eff,
            net_order_flow=int(self.rolling_buy_vol - self.rolling_sell_vol),
            order_flow_imbalance=round(self.ofi, 3),
            kyle_lambda=round(self.current_lambda, 8),
            cumulative_impact_pct=round(cum_impact, 3),
            spread_multiplier=self.get_spread_multiplier(),
            buy_pressure=round(self.rolling_buy_vol / tot_vol, 3),
            sell_pressure=round(self.rolling_sell_vol / tot_vol, 3),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert state to serializable dictionary."""
        t = self.get_telemetry()
        return {
            "baseline_price": t.baseline_price,
            "effective_mid": t.effective_mid,
            "net_order_flow": t.net_order_flow,
            "order_flow_imbalance": t.order_flow_imbalance,
            "kyle_lambda": t.kyle_lambda,
            "cumulative_impact_pct": t.cumulative_impact_pct,
            "spread_multiplier": t.spread_multiplier,
            "buy_pressure": t.buy_pressure,
            "sell_pressure": t.sell_pressure,
        }
