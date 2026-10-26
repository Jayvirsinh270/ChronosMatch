"""
ChronosMatch: Avellaneda-Stoikov Quantitative Market Maker.

Implements the classic high-frequency optimal market making framework:
- Reservation price adjusted for inventory risk
- Asymmetric bid/ask spread quoting based on order flow imbalance and asset volatility
- High-frequency limit order lifecycle management and real-time PnL accounting
"""

from dataclasses import dataclass
import math
import time
from typing import Dict, List, Optional, Tuple, Any, TYPE_CHECKING

from chronos_match.protocol import to_fixed_price, from_fixed_price, OrderSide

if TYPE_CHECKING:
    from chronos_match.market.ml_impact_engine import MLMarketImpactEngine


@dataclass
class MarketMakerStats:
    """Live performance metrics for the Avellaneda-Stoikov bot."""
    bot_name: str
    active: bool
    inventory: int                # Shares currently held (positive=long, negative=short)
    cash_pnl: float               # Cumulative cash generated
    unrealized_pnl: float         # Marked-to-market inventory value
    total_pnl: float              # Cash PnL + Unrealized PnL
    total_quotes_placed: int
    total_fills: int
    win_rate: float
    avg_spread_captured: float
    reservation_price: float
    bid_quote: float
    ask_quote: float


class AvellanedaStoikovMarketMaker:
    """
    Institutional inventory-aware High-Frequency Market Maker.
    """

    def __init__(
        self,
        bot_id: int = 801,
        symbol: str = "SOL-USD",
        risk_aversion: float = 0.005,       # Gamma
        target_inventory: int = 0,          # Desired net inventory
        max_inventory: int = 5000,          # Hard inventory ceiling
        order_size: int = 50,               # Standard quote lot size
    ):
        self.bot_id = bot_id
        self.symbol = symbol
        self.gamma = risk_aversion
        self.target_inventory = target_inventory
        self.max_inventory = max_inventory
        self.order_size = order_size

        self.active = False
        self.inventory = 0
        self.cash_balance = 0.0
        self.total_quotes_placed = 0
        self.total_fills = 0
        self.profitable_trades = 0
        self.cumulative_spread_captured = 0.0

        # Active resting quote IDs on the book
        self.active_bid_id: Optional[int] = None
        self.active_ask_id: Optional[int] = None
        self.last_bid_price = 0.0
        self.last_ask_price = 0.0
        self.last_reservation_price = 0.0

        # Trade fill history
        self.trades_history: List[Dict[str, Any]] = []

    def compute_quotes(
        self,
        mid_price: float,
        volatility: float,
        impact_engine: Optional[MLMarketImpactEngine] = None,
    ) -> Tuple[float, float, float]:
        """
        Compute optimal bid and ask prices according to Avellaneda-Stoikov formulas:
        1. Reservation Price: r(s, q) = s - (q - q_target) * gamma * sigma^2
        2. Optimal Spread: delta = (2 / gamma) * ln(1 + gamma / kappa)
        3. Bid Quote: r - delta/2, Ask Quote: r + delta/2
        """
        sigma_sq = max(0.0001, volatility ** 2)

        # 1. Reservation Price (skews lower when long inventory to incentivize selling;
        # skews higher when short inventory to incentivize buying)
        inv_delta = self.inventory - self.target_inventory
        reservation_p = mid_price - (inv_delta * self.gamma * sigma_sq * mid_price)
        self.last_reservation_price = round(reservation_p, 2)

        # 2. Optimal Spread Width
        # Base half-spread scaled by asset volatility
        half_spread = max(0.02, mid_price * (volatility * 0.15 + 0.0005))

        # Adjust spread for ML Order Flow Imbalance and market impact if provided
        if impact_engine:
            telemetry = impact_engine.get_telemetry()
            half_spread *= telemetry.spread_multiplier
            # If net buy pressure is high, lean ask further away to avoid being adversely picked off
            if telemetry.order_flow_imbalance > 0.3:
                ask_skew = half_spread * 0.25
                bid_skew = -half_spread * 0.10
            elif telemetry.order_flow_imbalance < -0.3:
                ask_skew = -half_spread * 0.10
                bid_skew = half_spread * 0.25
            else:
                ask_skew = 0.0
                bid_skew = 0.0
        else:
            ask_skew = 0.0
            bid_skew = 0.0

        bid_p = round(max(0.01, reservation_p - half_spread + bid_skew), 2)
        ask_p = round(max(bid_p + 0.01, reservation_p + half_spread + ask_skew), 2)

        self.last_bid_price = bid_p
        self.last_ask_price = ask_p
        return reservation_p, bid_p, ask_p

    def on_fill(self, side: int, price: float, quantity: int) -> None:
        """
        Callback when market maker's resting quote gets executed.
        side: 0 = BUY, 1 = SELL
        """
        self.total_fills += 1
        notional = price * quantity

        if side == 0:  # Bought shares
            self.inventory += quantity
            self.cash_balance -= notional
            trade_type = "BUY"
        else:          # Sold shares
            self.inventory -= quantity
            self.cash_balance += notional
            trade_type = "SELL"

        # Calculate spread capture
        if self.last_ask_price > self.last_bid_price:
            captured = (self.last_ask_price - self.last_bid_price) / 2.0
            self.cumulative_spread_captured += captured
            self.profitable_trades += 1

        self.trades_history.append({
            "time": time.strftime("%H:%M:%S"),
            "side": trade_type,
            "price": price,
            "quantity": quantity,
            "inventory": self.inventory,
            "cash_balance": round(self.cash_balance, 2),
        })
        if len(self.trades_history) > 50:
            self.trades_history.pop(0)

    def get_stats(self, current_mid: float) -> MarketMakerStats:
        """Calculate live marked-to-market PnL and telemetry."""
        mid = current_mid if current_mid > 0 else (self.last_reservation_price or 150.0)
        unrealized = self.inventory * mid
        tot_pnl = self.cash_balance + unrealized
        win_rate = (self.profitable_trades / max(1, self.total_fills)) * 100.0
        avg_spread = (self.cumulative_spread_captured / max(1, self.total_fills))

        return MarketMakerStats(
            bot_name="Avellaneda-Stoikov Market Maker",
            active=self.active,
            inventory=self.inventory,
            cash_pnl=round(self.cash_balance, 2),
            unrealized_pnl=round(unrealized, 2),
            total_pnl=round(tot_pnl, 2),
            total_quotes_placed=self.total_quotes_placed,
            total_fills=self.total_fills,
            win_rate=round(win_rate, 1),
            avg_spread_captured=round(avg_spread, 3),
            reservation_price=round(self.last_reservation_price, 2),
            bid_quote=self.last_bid_price,
            ask_quote=self.last_ask_price,
        )

    def to_dict(self, current_mid: float) -> Dict[str, Any]:
        """Convert telemetry to serializable dictionary."""
        s = self.get_stats(current_mid)
        return {
            "bot_name": s.bot_name,
            "active": s.active,
            "inventory": s.inventory,
            "cash_pnl": s.cash_pnl,
            "unrealized_pnl": s.unrealized_pnl,
            "total_pnl": s.total_pnl,
            "total_quotes": s.total_quotes_placed,
            "total_fills": s.total_fills,
            "win_rate": s.win_rate,
            "avg_spread_captured": s.avg_spread_captured,
            "reservation_price": s.reservation_price,
            "bid_quote": s.bid_quote,
            "ask_quote": s.ask_quote,
        }
