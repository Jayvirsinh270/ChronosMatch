"""
ChronosMatch: Quantitative Auto-Trading & Algorithmic Execution Suite.
"""

from typing import Dict, List, Optional, Any, Tuple

from chronos_match.algo.market_maker import (
    AvellanedaStoikovMarketMaker,
    MarketMakerStats,
)
from chronos_match.algo.momentum_bot import (
    MomentumBreakoutBot,
    MomentumBotStats,
)
from chronos_match.market.ml_impact_engine import MLMarketImpactEngine


class AlgoTradingManager:
    """
    Coordinates and executes institutional algorithmic strategies across venues.
    """

    def __init__(self, symbol: str = "SOL-USD"):
        self.symbol = symbol
        self.market_maker = AvellanedaStoikovMarketMaker(bot_id=801, symbol=symbol)
        self.momentum_bot = MomentumBreakoutBot(bot_id=802, symbol=symbol)

    def set_symbol(self, symbol: str) -> None:
        """Update target symbol for algorithmic trading."""
        self.symbol = symbol
        self.market_maker.symbol = symbol
        self.momentum_bot.symbol = symbol

    def toggle_bot(self, bot_type: str, enable: Optional[bool] = None) -> bool:
        """Toggle an algorithm on or off ('mm' for Market Maker, 'momentum' for Momentum)."""
        btype = bot_type.lower()
        if btype in ("mm", "market_maker"):
            self.market_maker.active = not self.market_maker.active if enable is None else enable
            return self.market_maker.active
        elif btype in ("momentum", "trend"):
            self.momentum_bot.active = not self.momentum_bot.active if enable is None else enable
            return self.momentum_bot.active
        return False

    def on_trade_executed(self, client_id: int, side: int, price: float, quantity: int) -> None:
        """Deliver trade fill callbacks to corresponding algorithm."""
        if client_id == self.market_maker.bot_id:
            self.market_maker.on_fill(side, price, quantity)

    def get_summary(self, current_mid: float) -> Dict[str, Any]:
        """Aggregate performance telemetry for the web dashboard."""
        mm_data = self.market_maker.to_dict(current_mid)
        mom_data = self.momentum_bot.to_dict(current_mid)
        total_pnl = mm_data["total_pnl"] + mom_data["total_pnl"]

        return {
            "symbol": self.symbol,
            "total_algorithmic_pnl": round(total_pnl, 2),
            "market_maker": mm_data,
            "momentum_bot": mom_data,
        }


__all__ = [
    "AvellanedaStoikovMarketMaker",
    "MarketMakerStats",
    "MomentumBreakoutBot",
    "MomentumBotStats",
    "AlgoTradingManager",
]
