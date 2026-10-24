"""
ChronosMatch: Trend-Following Momentum & Order Flow Breakout Bot.

Identifies microstructural momentum bursts by combining:
- Fast & slow Exponential Moving Average (EMA) crossovers
- Real-time Order Flow Imbalance (OFI) from the ML impact engine
- Dynamic trailing stops and profit targets
"""

from dataclasses import dataclass
import time
from typing import Dict, List, Optional, Tuple, Any

from chronos_match.protocol import to_fixed_price, from_fixed_price, OrderSide


@dataclass
class MomentumBotStats:
    bot_name: str
    active: bool
    position: int
    entry_price: float
    cash_pnl: float
    unrealized_pnl: float
    total_pnl: float
    total_trades: int
    winning_trades: int
    win_rate: float
    fast_ema: float
    slow_ema: float
    signal: str  # "LONG", "SHORT", or "NEUTRAL"


class MomentumBreakoutBot:
    """
    Directional quantitative taker trading on order flow momentum.
    """

    def __init__(
        self,
        bot_id: int = 802,
        symbol: str = "SOL-USD",
        fast_period: int = 5,
        slow_period: int = 15,
        trade_size: int = 25,
        profit_target_pct: float = 0.015,  # 1.5% take profit
        stop_loss_pct: float = 0.008,      # 0.8% stop loss
    ):
        self.bot_id = bot_id
        self.symbol = symbol
        self.fast_alpha = 2.0 / (fast_period + 1)
        self.slow_alpha = 2.0 / (slow_period + 1)
        self.trade_size = trade_size
        self.profit_target_pct = profit_target_pct
        self.stop_loss_pct = stop_loss_pct

        self.active = False
        self.position = 0          # > 0 long, < 0 short
        self.entry_price = 0.0
        self.cash_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0

        self.fast_ema: Optional[float] = None
        self.slow_ema: Optional[float] = None
        self.current_signal = "NEUTRAL"

    def update_tick(self, price: float, order_flow_imbalance: float = 0.0) -> Optional[Tuple[str, int, float]]:
        """
        Feed live tick price and OFI into the momentum model.
        Returns trade recommendation: (action: "BUY"|"SELL", quantity, price) or None.
        """
        if price <= 0:
            return None

        # Initialize EMAs
        if self.fast_ema is None:
            self.fast_ema = price
            self.slow_ema = price
            return None

        # Update rolling EMAs
        self.fast_ema = (price * self.fast_alpha) + (self.fast_ema * (1.0 - self.fast_alpha))
        self.slow_ema = (price * self.slow_alpha) + (self.slow_ema * (1.0 - self.slow_alpha))

        if not self.active:
            return None

        # Check existing open position exit rules (Take Profit & Stop Loss)
        if self.position > 0:  # Currently Long
            pnl_pct = (price - self.entry_price) / self.entry_price
            if pnl_pct >= self.profit_target_pct or pnl_pct <= -self.stop_loss_pct:
                qty = self.position
                self._close_position(price)
                return ("SELL", qty, price)

        elif self.position < 0:  # Currently Short
            pnl_pct = (self.entry_price - price) / self.entry_price
            if pnl_pct >= self.profit_target_pct or pnl_pct <= -self.stop_loss_pct:
                qty = abs(self.position)
                self._close_position(price)
                return ("BUY", qty, price)

        # Check Entry Signals if flat
        if self.position == 0:
            # Bullish Breakout: Fast EMA above Slow EMA and positive order flow pressure
            if self.fast_ema > self.slow_ema * 1.0005 and order_flow_imbalance > 0.10:
                self.current_signal = "LONG"
                self.position = self.trade_size
                self.entry_price = price
                self.cash_pnl -= price * self.trade_size
                self.total_trades += 1
                return ("BUY", self.trade_size, price)

            # Bearish Breakout: Fast EMA below Slow EMA and negative order flow pressure
            elif self.fast_ema < self.slow_ema * 0.9995 and order_flow_imbalance < -0.10:
                self.current_signal = "SHORT"
                self.position = -self.trade_size
                self.entry_price = price
                self.cash_pnl += price * self.trade_size
                self.total_trades += 1
                return ("SELL", self.trade_size, price)

        return None

    def _close_position(self, exit_price: float) -> None:
        """Close existing position and record PnL."""
        if self.position > 0:
            trade_profit = (exit_price - self.entry_price) * self.position
            self.cash_pnl += exit_price * self.position
        else:
            trade_profit = (self.entry_price - exit_price) * abs(self.position)
            self.cash_pnl -= exit_price * abs(self.position)

        if trade_profit > 0:
            self.winning_trades += 1

        self.position = 0
        self.entry_price = 0.0
        self.current_signal = "NEUTRAL"

    def get_stats(self, current_price: float) -> MomentumBotStats:
        """Compute live marked-to-market performance."""
        p = current_price if current_price > 0 else (self.entry_price or 150.0)
        unrealized = self.position * p
        tot_pnl = self.cash_pnl + unrealized
        win_rate = (self.winning_trades / max(1, self.total_trades)) * 100.0

        return MomentumBotStats(
            bot_name="Order Flow Momentum Bot",
            active=self.active,
            position=self.position,
            entry_price=round(self.entry_price, 2),
            cash_pnl=round(self.cash_pnl, 2),
            unrealized_pnl=round(unrealized, 2),
            total_pnl=round(tot_pnl, 2),
            total_trades=self.total_trades,
            winning_trades=self.winning_trades,
            win_rate=round(win_rate, 1),
            fast_ema=round(self.fast_ema or p, 2),
            slow_ema=round(self.slow_ema or p, 2),
            signal=self.current_signal,
        )

    def to_dict(self, current_price: float) -> Dict[str, Any]:
        """Convert state to serializable dictionary."""
        s = self.get_stats(current_price)
        return {
            "bot_name": s.bot_name,
            "active": s.active,
            "position": s.position,
            "entry_price": s.entry_price,
            "cash_pnl": s.cash_pnl,
            "unrealized_pnl": s.unrealized_pnl,
            "total_pnl": s.total_pnl,
            "total_trades": s.total_trades,
            "win_rate": s.win_rate,
            "fast_ema": s.fast_ema,
            "slow_ema": s.slow_ema,
            "signal": s.signal,
        }
