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
from chronos_match.algo.rl_execution_agent import (
    RLExecutionAgent,
    ExecutionAction,
    ParentOrderState,
    ChildExecutionFill,
)


class AlgoTradingManager:
    """
    Coordinates and executes institutional algorithmic strategies across venues.
    """

    def __init__(self, symbol: str = "SOL-USD"):
        self.symbol = symbol
        self.market_maker = AvellanedaStoikovMarketMaker(bot_id=801, symbol=symbol)
        self.momentum_bot = MomentumBreakoutBot(bot_id=802, symbol=symbol)
        self.rl_execution_agent = RLExecutionAgent(bot_id=803, symbol=symbol)

    def set_symbol(self, symbol: str) -> None:
        """Update target symbol for algorithmic trading."""
        self.symbol = symbol
        self.market_maker.symbol = symbol
        self.momentum_bot.symbol = symbol
        self.rl_execution_agent.symbol = symbol

    def toggle_bot(self, bot_type: str, enable: Optional[bool] = None) -> bool:
        """Toggle an algorithm on or off ('mm' for Market Maker, 'momentum' for Momentum, 'rl' for RL Execution Agent)."""
        btype = bot_type.lower()
        if btype in ("mm", "market_maker"):
            self.market_maker.active = not self.market_maker.active if enable is None else enable
            return self.market_maker.active
        elif btype in ("momentum", "trend"):
            self.momentum_bot.active = not self.momentum_bot.active if enable is None else enable
            return self.momentum_bot.active
        elif btype in ("rl", "rl_execution", "rl_agent"):
            self.rl_execution_agent.active = not self.rl_execution_agent.active if enable is None else enable
            return self.rl_execution_agent.active
        return False

    def on_trade_executed(
        self,
        client_id: int,
        side: int,
        price: float,
        quantity: int,
        is_maker: bool = False,
        next_state: Optional[List[float]] = None,
    ) -> None:
        """Deliver trade fill callbacks to corresponding algorithm."""
        if client_id == self.market_maker.bot_id:
            self.market_maker.on_fill(side, price, quantity)
        elif client_id == self.rl_execution_agent.bot_id:
            self.rl_execution_agent.record_execution_fill(
                exec_price=price,
                quantity=quantity,
                is_maker=is_maker,
                next_state=next_state,
            )

    def get_summary(self, current_mid: float) -> Dict[str, Any]:
        """Aggregate performance telemetry for the web dashboard."""
        mm_data = self.market_maker.to_dict(current_mid)
        mom_data = self.momentum_bot.to_dict(current_mid)
        rl_data = self.rl_execution_agent.to_dict(current_mid)
        total_pnl = mm_data["total_pnl"] + mom_data["total_pnl"]

        return {
            "symbol": self.symbol,
            "total_algorithmic_pnl": round(total_pnl, 2),
            "market_maker": mm_data,
            "momentum_bot": mom_data,
            "rl_execution_agent": rl_data,
        }


from chronos_match.algo.risk_metrics import (
    TearSheetMetrics,
    generate_tear_sheet,
    calculate_returns,
    cagr,
    annualized_volatility,
    sharpe_ratio,
    sortino_ratio,
    max_drawdown,
    calmar_ratio,
    value_at_risk,
    conditional_var,
    beta_and_alpha,
    calculate_trade_statistics,
)
from chronos_match.algo.monte_carlo import (
    MonteCarloEngine,
    MonteCarloResult,
)
from chronos_match.algo.backtester import (
    BacktestEngine,
    BacktestResult,
    BacktestTrade,
    BaseStrategy,
    DualEMABreakoutStrategy,
    MeanReversionBollingerStrategy,
    BayesianOrderFlowStrategy,
    RLTWAPExecutionBenchmark,
)


__all__ = [
    "AvellanedaStoikovMarketMaker",
    "MarketMakerStats",
    "MomentumBreakoutBot",
    "MomentumBotStats",
    "RLExecutionAgent",
    "ExecutionAction",
    "ParentOrderState",
    "ChildExecutionFill",
    "AlgoTradingManager",
    "TearSheetMetrics",
    "generate_tear_sheet",
    "calculate_returns",
    "cagr",
    "annualized_volatility",
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown",
    "calmar_ratio",
    "value_at_risk",
    "conditional_var",
    "beta_and_alpha",
    "calculate_trade_statistics",
    "MonteCarloEngine",
    "MonteCarloResult",
    "BacktestEngine",
    "BacktestResult",
    "BacktestTrade",
    "BaseStrategy",
    "DualEMABreakoutStrategy",
    "MeanReversionBollingerStrategy",
    "BayesianOrderFlowStrategy",
    "RLTWAPExecutionBenchmark",
]
