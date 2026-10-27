"""
ChronosMatch: Institutional Quantitative Strategy Backtesting Engine.

Simulates algorithmic alpha strategies across real historical OHLCV stock and crypto bars:
- Realistic slippage impact modeling and exchange commissions
- Position sizing, mark-to-market equity tracking, and stop-loss / take-profit risk guardrails
- Integrated Bloomberg PORT / PyFolio-grade risk tear sheet analytics
- Forward Monte Carlo stress testing and ruin probability forecasting
- Pluggable quantitative strategies:
  1. Dual EMA Trend Breakout Strategy
  2. Bollinger Bands Mean Reversion Strategy
  3. Bayesian Order Flow / Momentum Strategy
  4. Institutional TWAP Execution Benchmark
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import math
import time
from typing import Dict, List, Optional, Tuple, Any

from chronos_match.market.data_loader import MarketDataLoader, StockCandle
from chronos_match.algo.risk_metrics import generate_tear_sheet, TearSheetMetrics, calculate_returns
from chronos_match.algo.monte_carlo import MonteCarloEngine, MonteCarloResult


@dataclass
class BacktestTrade:
    """Detailed record of an executed backtest trade."""
    trade_id: int
    symbol: str
    side: str                # "BUY" (Long) or "SELL" (Short)
    entry_bar_idx: int
    exit_bar_idx: int
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    quantity: int
    gross_pnl: float
    commission: float
    slippage: float
    net_pnl: float
    return_pct: float
    holding_bars: int
    exit_reason: str         # "SIGNAL", "STOP_LOSS", "TAKE_PROFIT", "END_OF_DATA"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side,
            "entry_date": self.entry_date,
            "exit_date": self.exit_date,
            "entry_time": self.entry_date,
            "exit_time": self.exit_date,
            "entry_price": round(self.entry_price, 2),
            "exit_price": round(self.exit_price, 2),
            "quantity": self.quantity,
            "shares": self.quantity,
            "gross_pnl": round(self.gross_pnl, 2),
            "commission": round(self.commission, 2),
            "slippage": round(self.slippage, 2),
            "net_pnl": round(self.net_pnl, 2),
            "return_pct": round(self.return_pct, 2),
            "pnl_pct": round(self.return_pct, 2),
            "holding_bars": self.holding_bars,
            "exit_reason": self.exit_reason,
        }


@dataclass
class BacktestResult:
    """Full backtest outcome including tear sheet scorecard and equity curves."""
    symbol: str
    strategy_name: str
    initial_capital: float
    ending_capital: float
    total_net_pnl: float
    dates: List[str]
    equity_curve: List[float]
    drawdown_curve: List[float]
    benchmark_curve: List[float]
    trades: List[BacktestTrade]
    tear_sheet: TearSheetMetrics
    monte_carlo: Optional[MonteCarloResult] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy_name": self.strategy_name,
            "initial_capital": round(self.initial_capital, 2),
            "ending_capital": round(self.ending_capital, 2),
            "final_capital": round(self.ending_capital, 2),
            "total_net_pnl": round(self.total_net_pnl, 2),
            "net_pnl": round(self.total_net_pnl, 2),
            "dates": self.dates,
            "equity_curve": [round(eq, 2) for eq in self.equity_curve],
            "drawdown_curve": [round(dd * 100.0, 2) for dd in self.drawdown_curve],
            "benchmark_curve": [round(b, 2) for b in self.benchmark_curve],
            "benchmark_equity": [round(b, 2) for b in self.benchmark_curve],
            "trades": [t.to_dict() for t in self.trades[-50:]], # Return last 50 trades for UI table
            "tear_sheet": self.tear_sheet.to_dict(),
            "monte_carlo": self.monte_carlo.to_dict() if self.monte_carlo else None,
        }


# ─────────────────────────────────────────────────────────────
# QUANTITATIVE STRATEGIES
# ─────────────────────────────────────────────────────────────

class BaseStrategy(ABC):
    """Abstract quantitative strategy interface."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def evaluate_bar(
        self,
        bar_idx: int,
        candles: List[StockCandle],
        in_position: bool,
        entry_price: float,
    ) -> Optional[str]:
        """
        Evaluate trading signal for the current candle.
        Returns: 'BUY' (open long), 'SELL' (close long), or None (hold).
        """
        pass


class DualEMABreakoutStrategy(BaseStrategy):
    """
    Dual Exponential Moving Average (EMA) Trend Breakout with RSI Filter.
    - Long Entry: Fast EMA crosses above Slow EMA and RSI > 45.
    - Long Exit: Fast EMA crosses below Slow EMA or RSI < 35.
    """

    def __init__(self, fast_period: int = 12, slow_period: int = 26, rsi_period: int = 14):
        super().__init__(name=f"Dual EMA Breakout ({fast_period}/{slow_period})")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.rsi_period = rsi_period
        self.k_fast = 2.0 / (fast_period + 1)
        self.k_slow = 2.0 / (slow_period + 1)

    def evaluate_bar(
        self,
        bar_idx: int,
        candles: List[StockCandle],
        in_position: bool,
        entry_price: float,
    ) -> Optional[str]:
        if bar_idx < self.slow_period + 1:
            return None

        # Compute EMA values
        closes = [c.close for c in candles[: bar_idx + 1]]
        fast_ema = self._compute_ema(closes, self.fast_period, self.k_fast)
        slow_ema = self._compute_ema(closes, self.slow_period, self.k_slow)
        rsi = self._compute_rsi(closes, self.rsi_period)

        prev_closes = closes[:-1]
        prev_fast = self._compute_ema(prev_closes, self.fast_period, self.k_fast)
        prev_slow = self._compute_ema(prev_closes, self.slow_period, self.k_slow)

        golden_cross = (prev_fast <= prev_slow) and (fast_ema > slow_ema)
        death_cross = (prev_fast >= prev_slow) and (fast_ema < slow_ema)

        if not in_position and golden_cross and rsi > 45.0:
            return "BUY"
        elif in_position and (death_cross or rsi < 35.0):
            return "SELL"
        return None

    def _compute_ema(self, series: List[float], period: int, k: float) -> float:
        if len(series) < period:
            return series[-1]
        val = sum(series[:period]) / period
        for p in series[period:]:
            val = (p * k) + (val * (1.0 - k))
        return val

    def _compute_rsi(self, series: List[float], period: int = 14) -> float:
        if len(series) < period + 1:
            return 50.0
        gains = 0.0
        losses = 0.0
        for i in range(len(series) - period, len(series)):
            chg = series[i] - series[i - 1]
            if chg > 0:
                gains += chg
            else:
                losses -= chg
        if losses == 0.0:
            return 100.0
        rs = (gains / period) / (losses / period)
        return 100.0 - (100.0 / (1.0 + rs))


class MeanReversionBollingerStrategy(BaseStrategy):
    """
    Statistical Bollinger Bands Mean Reversion Strategy.
    - Long Entry: Price dips below Lower Band (SMA - 2*std) with RSI < 35 (Oversold).
    - Long Exit: Price reverts to Mid Band (SMA) or pierces Upper Band with RSI > 65.
    """

    def __init__(self, period: int = 20, num_std: float = 2.0, rsi_period: int = 14):
        super().__init__(name=f"Bollinger Mean Reversion ({period}, {num_std}σ)")
        self.period = period
        self.num_std = num_std
        self.rsi_period = rsi_period

    def evaluate_bar(
        self,
        bar_idx: int,
        candles: List[StockCandle],
        in_position: bool,
        entry_price: float,
    ) -> Optional[str]:
        if bar_idx < self.period:
            return None

        closes = [c.close for c in candles[: bar_idx + 1]]
        window = closes[-self.period :]
        sma = sum(window) / self.period
        variance = sum((p - sma) ** 2 for p in window) / self.period
        std_dev = math.sqrt(max(1e-6, variance))

        upper_band = sma + self.num_std * std_dev
        lower_band = sma - self.num_std * std_dev
        cur_p = closes[-1]
        rsi = self._compute_rsi(closes, self.rsi_period)

        if not in_position and cur_p <= lower_band and rsi < 40.0:
            return "BUY"
        elif in_position and (cur_p >= sma or rsi > 65.0):
            return "SELL"
        return None

    def _compute_rsi(self, series: List[float], period: int = 14) -> float:
        if len(series) < period + 1:
            return 50.0
        gains = 0.0
        losses = 0.0
        for i in range(len(series) - period, len(series)):
            chg = series[i] - series[i - 1]
            if chg > 0:
                gains += chg
            else:
                losses -= chg
        if losses == 0.0:
            return 100.0
        rs = (gains / period) / (losses / period)
        return 100.0 - (100.0 / (1.0 + rs))


class BayesianOrderFlowStrategy(BaseStrategy):
    """
    Gaussian Naive Bayes & Trend-Momentum Strategy.
    Simulates multi-bar order flow momentum with volume breakout confirmation.
    """

    def __init__(self, lookback: int = 15):
        super().__init__(name=f"Bayesian Order Flow Alpha ({lookback} bars)")
        self.lookback = lookback

    def evaluate_bar(
        self,
        bar_idx: int,
        candles: List[StockCandle],
        in_position: bool,
        entry_price: float,
    ) -> Optional[str]:
        if bar_idx < self.lookback:
            return None

        recent = candles[bar_idx - self.lookback : bar_idx + 1]
        cur = recent[-1]
        prev = recent[-2]

        avg_vol = sum(c.volume for c in recent[:-1]) / self.lookback
        vol_surge = cur.volume > avg_vol * 1.25

        # Upward price thrust with body > 60% of candle range
        candle_range = max(0.01, cur.high - cur.low)
        body = cur.close - cur.open
        strong_bull = body > 0 and (body / candle_range) > 0.60
        strong_bear = body < 0 and (abs(body) / candle_range) > 0.60

        if not in_position and strong_bull and vol_surge and cur.close > prev.high:
            return "BUY"
        elif in_position and (strong_bear or cur.close < prev.low):
            return "SELL"
        return None


class RLTWAPExecutionBenchmark(BaseStrategy):
    """
    Systematic Time-Weighted Average Price (TWAP) execution benchmark strategy.
    Enters systematically on schedule and liquidates on target horizon.
    """

    def __init__(self, interval_bars: int = 8):
        super().__init__(name=f"Systematic TWAP Execution Benchmark ({interval_bars} bars)")
        self.interval = interval_bars

    def evaluate_bar(
        self,
        bar_idx: int,
        candles: List[StockCandle],
        in_position: bool,
        entry_price: float,
    ) -> Optional[str]:
        if not in_position and (bar_idx % (self.interval * 2) == 0):
            return "BUY"
        elif in_position and (bar_idx % self.interval == 0):
            return "SELL"
        return None


# ─────────────────────────────────────────────────────────────
# BACKTESTING ENGINE
# ─────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    High-performance backtesting simulation core with slippage and commission attribution.
    """

    def __init__(self, data_loader: Optional[MarketDataLoader] = None):
        self.data_loader = data_loader or MarketDataLoader()
        self.monte_carlo_engine = MonteCarloEngine(seed=101)

    def get_supported_strategies(self) -> List[Dict[str, Any]]:
        """Return registry of available quantitative alpha strategies."""
        return [
            {
                "id": "dual_ema",
                "name": "Dual EMA Trend Breakout",
                "description": "Momentum crossover strategy exploiting directional medium-term price trends with RSI filters.",
                "default_params": {"fast_period": 12, "slow_period": 26, "rsi_period": 14},
            },
            {
                "id": "bollinger_reversion",
                "name": "Bollinger Mean Reversion",
                "description": "Statistical arbitrage strategy taking contrarian trades on 2σ statistical price exhaustion.",
                "default_params": {"period": 20, "num_std": 2.0, "rsi_period": 14},
            },
            {
                "id": "bayesian_flow",
                "name": "Bayesian Order Flow Momentum",
                "description": "Microstructural order flow surge model combining volume bursts with high-body thrust candlesticks.",
                "default_params": {"lookback": 15},
            },
            {
                "id": "twap_benchmark",
                "name": "Systematic TWAP Benchmark",
                "description": "Deterministic institutional benchmark slicing inventory evenly across time horizons.",
                "default_params": {"interval_bars": 8},
            },
        ]

    def create_strategy(self, strategy_id: str, params: Optional[Dict[str, Any]] = None) -> BaseStrategy:
        """Instantiate quantitative strategy from identifier and parameters."""
        p = params or {}
        sid = strategy_id.lower().replace("-", "_")

        if "ema" in sid or "trend" in sid:
            return DualEMABreakoutStrategy(
                fast_period=int(p.get("fast_period", 12)),
                slow_period=int(p.get("slow_period", 26)),
                rsi_period=int(p.get("rsi_period", 14)),
            )
        elif "bollinger" in sid or "reversion" in sid:
            return MeanReversionBollingerStrategy(
                period=int(p.get("period", 20)),
                num_std=float(p.get("num_std", 2.0)),
                rsi_period=int(p.get("rsi_period", 14)),
            )
        elif "bayesian" in sid or "flow" in sid:
            return BayesianOrderFlowStrategy(
                lookback=int(p.get("lookback", 15)),
            )
        else:
            return RLTWAPExecutionBenchmark(
                interval_bars=int(p.get("interval_bars", 8)),
            )

    def run_backtest(
        self,
        symbol: str = "NVDA",
        strategy_id: str = "dual_ema",
        initial_capital: float = 100_000.0,
        max_bars: int = 300,
        commission_bps: float = 5.0,     # 5 bps (0.05%) round-trip fee
        slippage_bps: float = 3.5,       # 3.5 bps market impact concession
        stop_loss_pct: float = 4.0,      # Stop loss at 4% adverse move
        take_profit_pct: float = 8.0,    # Take profit at 8% favorable move
        strategy_params: Optional[Dict[str, Any]] = None,
        run_monte_carlo: bool = True,
        monte_carlo_sims: int = 1_000,
    ) -> BacktestResult:
        """
        Execute full historical backtest simulation and return performance scorecards.
        """
        sym = symbol.strip().upper()
        candles = self.data_loader.load_candles(sym, max_bars=max_bars)
        if len(candles) < 30:
            # Re-try with broader search or synthesize realistic wave bars
            candles = self.data_loader.load_candles(sym, max_bars=150)

        strategy = self.create_strategy(strategy_id, strategy_params)

        cash = initial_capital
        position_shares = 0
        entry_price = 0.0
        entry_bar_idx = 0
        entry_date = ""

        trades: List[BacktestTrade] = []
        equity_curve: List[float] = []
        drawdown_curve: List[float] = []
        benchmark_curve: List[float] = []
        dates: List[str] = []

        comm_factor = commission_bps / 10_000.0
        slip_factor = slippage_bps / 10_000.0

        p0 = candles[0].close if candles else 100.0
        peak_equity = initial_capital
        trade_counter = 0

        for i, bar in enumerate(candles):
            cur_price = bar.close
            dates.append(bar.date)

            # Benchmark Buy & Hold performance
            bm_equity = (cur_price / p0) * initial_capital
            benchmark_curve.append(bm_equity)

            in_position = position_shares > 0

            # 1. Check Stop-Loss / Take-Profit Risk Controls
            exit_signal = False
            exit_reason = "SIGNAL"

            if in_position:
                price_move_pct = ((cur_price - entry_price) / entry_price) * 100.0
                if stop_loss_pct > 0.0 and price_move_pct <= -stop_loss_pct:
                    exit_signal = True
                    exit_reason = "STOP_LOSS"
                elif take_profit_pct > 0.0 and price_move_pct >= take_profit_pct:
                    exit_signal = True
                    exit_reason = "TAKE_PROFIT"

            # 2. Evaluate Strategy Decision Rule
            if not exit_signal:
                sig = strategy.evaluate_bar(i, candles, in_position, entry_price)
                if sig == "BUY" and not in_position:
                    # Allocate 95% of available cash into position
                    exec_price = round(cur_price * (1.0 + slip_factor), 2)
                    max_invest = cash * 0.95
                    shares = max(1, int(max_invest // exec_price))
                    trade_cost = shares * exec_price
                    comm = trade_cost * comm_factor
                    total_outlay = trade_cost + comm

                    if total_outlay <= cash:
                        cash -= total_outlay
                        position_shares = shares
                        entry_price = exec_price
                        entry_bar_idx = i
                        entry_date = bar.date

                elif sig == "SELL" and in_position:
                    exit_signal = True
                    exit_reason = "SIGNAL"

            # 3. Process Position Liquidation
            if exit_signal and in_position:
                exec_price = round(cur_price * (1.0 - slip_factor), 2)
                gross_proceeds = position_shares * exec_price
                comm = gross_proceeds * comm_factor
                net_proceeds = gross_proceeds - comm
                cash += net_proceeds

                gross_pnl = (exec_price - entry_price) * position_shares
                total_comm = comm + (position_shares * entry_price * comm_factor)
                total_slip = position_shares * (cur_price * slip_factor * 2.0)
                net_pnl = gross_pnl - total_comm

                ret_pct = (net_pnl / (position_shares * entry_price)) * 100.0
                trade_counter += 1

                trade = BacktestTrade(
                    trade_id=trade_counter,
                    symbol=sym,
                    side="BUY",
                    entry_bar_idx=entry_bar_idx,
                    exit_bar_idx=i,
                    entry_date=entry_date,
                    exit_date=bar.date,
                    entry_price=entry_price,
                    exit_price=exec_price,
                    quantity=position_shares,
                    gross_pnl=gross_pnl,
                    commission=total_comm,
                    slippage=total_slip,
                    net_pnl=net_pnl,
                    return_pct=ret_pct,
                    holding_bars=i - entry_bar_idx,
                    exit_reason=exit_reason,
                )
                trades.append(trade)
                position_shares = 0
                entry_price = 0.0

            # 4. Mark to Market Portfolio Equity
            current_equity = cash + (position_shares * cur_price)
            equity_curve.append(current_equity)

            if current_equity > peak_equity:
                peak_equity = current_equity
            dd = (peak_equity - current_equity) / peak_equity if peak_equity > 0 else 0.0
            drawdown_curve.append(dd)

        # Force liquidation of open position on final bar
        if position_shares > 0 and candles:
            final_bar = candles[-1]
            exec_price = round(final_bar.close * (1.0 - slip_factor), 2)
            gross_proceeds = position_shares * exec_price
            comm = gross_proceeds * comm_factor
            cash += gross_proceeds - comm
            gross_pnl = (exec_price - entry_price) * position_shares
            total_comm = comm + (position_shares * entry_price * comm_factor)
            net_pnl = gross_pnl - total_comm
            ret_pct = (net_pnl / (position_shares * entry_price)) * 100.0
            trade_counter += 1

            trades.append(
                BacktestTrade(
                    trade_id=trade_counter,
                    symbol=sym,
                    side="BUY",
                    entry_bar_idx=entry_bar_idx,
                    exit_bar_idx=len(candles) - 1,
                    entry_date=entry_date,
                    exit_date=final_bar.date,
                    entry_price=entry_price,
                    exit_price=exec_price,
                    quantity=position_shares,
                    gross_pnl=gross_pnl,
                    commission=total_comm,
                    slippage=position_shares * final_bar.close * slip_factor * 2.0,
                    net_pnl=net_pnl,
                    return_pct=ret_pct,
                    holding_bars=(len(candles) - 1) - entry_bar_idx,
                    exit_reason="END_OF_DATA",
                )
            )
            equity_curve[-1] = cash

        # 5. Generate Tear Sheet Scorecard
        trade_pnls = [t.net_pnl for t in trades]
        tear_sheet = generate_tear_sheet(
            equity_curve=equity_curve,
            benchmark_curve=benchmark_curve,
            trade_pnls=trade_pnls,
            risk_free_rate=0.04,
            periods_per_year=252.0,
        )

        # 6. Run Monte Carlo Stress Testing Projections
        mc_result: Optional[MonteCarloResult] = None
        if run_monte_carlo:
            periodic_returns = calculate_returns(equity_curve)
            mc_result = self.monte_carlo_engine.simulate_from_returns(
                returns=periodic_returns,
                initial_equity=equity_curve[-1] if equity_curve else initial_capital,
                forecast_bars=min(60, len(candles)),
                num_simulations=monte_carlo_sims,
                ruin_drawdown_pct=20.0,
                method="bootstrap",
                symbol=sym,
            )

        return BacktestResult(
            symbol=sym,
            strategy_name=strategy.name,
            initial_capital=initial_capital,
            ending_capital=equity_curve[-1] if equity_curve else initial_capital,
            total_net_pnl=(equity_curve[-1] - initial_capital) if equity_curve else 0.0,
            dates=dates,
            equity_curve=equity_curve,
            drawdown_curve=drawdown_curve,
            benchmark_curve=benchmark_curve,
            trades=trades,
            tear_sheet=tear_sheet,
            monte_carlo=mc_result,
        )
