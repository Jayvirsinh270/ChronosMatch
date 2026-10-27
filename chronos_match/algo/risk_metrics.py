"""
ChronosMatch: Institutional Quantitative Risk & Performance Metrics Engine.

Implements Bloomberg PORT / PyFolio institutional-grade tear sheet statistics:
- Sharpe Ratio, Sortino Ratio, Calmar Ratio
- Maximum Drawdown (MDD), Peak-to-Trough duration, Recovery duration
- Parametric & Historical Value at Risk (VaR 95% & 99%)
- Conditional Value at Risk (CVaR / Expected Shortfall)
- Beta and Jensen's Alpha against Benchmark
- Win/Loss Ratio, Profit Factor, Expectancy, Payoff Ratio
- Pure Python high-performance vectorized computations (zero external dependencies).
"""

from dataclasses import dataclass, field
import math
from typing import Dict, List, Optional, Tuple, Any


@dataclass
class TearSheetMetrics:
    """Institutional quantitative risk and return scorecard."""
    initial_capital: float
    ending_capital: float
    total_net_pnl: float
    cumulative_return_pct: float
    cagr_pct: float
    annualized_volatility_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown_pct: float
    max_drawdown_duration_bars: int
    var_95_pct: float
    var_99_pct: float
    cvar_95_pct: float
    cvar_99_pct: float
    beta: float
    alpha_pct: float
    benchmark_return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    profit_factor: float
    payoff_ratio: float
    average_trade_pnl: float
    largest_winning_trade: float
    largest_losing_trade: float
    max_consecutive_wins: int
    max_consecutive_losses: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "initial_capital": round(self.initial_capital, 2),
            "ending_capital": round(self.ending_capital, 2),
            "total_net_pnl": round(self.total_net_pnl, 2),
            "cumulative_return_pct": round(self.cumulative_return_pct, 2),
            "total_return_pct": round(self.cumulative_return_pct, 2),
            "cagr_pct": round(self.cagr_pct, 2),
            "annualized_volatility_pct": round(self.annualized_volatility_pct, 2),
            "annualized_vol_pct": round(self.annualized_volatility_pct, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 2),
            "sortino_ratio": round(self.sortino_ratio, 2),
            "calmar_ratio": round(self.calmar_ratio, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "max_drawdown_duration_bars": self.max_drawdown_duration_bars,
            "max_drawdown_duration": self.max_drawdown_duration_bars,
            "var_95_pct": round(self.var_95_pct, 2),
            "var_99_pct": round(self.var_99_pct, 2),
            "cvar_95_pct": round(self.cvar_95_pct, 2),
            "cvar_99_pct": round(self.cvar_99_pct, 2),
            "beta": round(self.beta, 2),
            "alpha_pct": round(self.alpha_pct, 2),
            "alpha_annualized_pct": round(self.alpha_pct, 2),
            "benchmark_return_pct": round(self.benchmark_return_pct, 2),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": round(self.win_rate_pct, 1),
            "profit_factor": round(self.profit_factor, 2),
            "payoff_ratio": round(self.payoff_ratio, 2),
            "average_trade_pnl": round(self.average_trade_pnl, 2),
            "largest_winning_trade": round(self.largest_winning_trade, 2),
            "largest_losing_trade": round(self.largest_losing_trade, 2),
            "max_consecutive_wins": self.max_consecutive_wins,
            "max_consecutive_losses": self.max_consecutive_losses,
        }


def calculate_returns(equity_curve: List[float]) -> List[float]:
    """Calculate periodic fractional returns from an equity curve: r_t = (E_t - E_{t-1}) / E_{t-1}."""
    if len(equity_curve) < 2:
        return []
    returns: List[float] = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        cur = equity_curve[i]
        if prev > 0.0:
            returns.append((cur - prev) / prev)
        else:
            returns.append(0.0)
    return returns


def cagr(equity_curve: List[float], periods_per_year: float = 252.0) -> float:
    """Compound Annual Growth Rate (CAGR)."""
    if len(equity_curve) < 2 or equity_curve[0] <= 0.0:
        return 0.0
    total_periods = len(equity_curve) - 1
    total_mult = equity_curve[-1] / equity_curve[0]
    if total_mult <= 0.0:
        return -1.0
    years = total_periods / max(1.0, periods_per_year)
    if years <= 0.0:
        return 0.0
    return (total_mult ** (1.0 / years)) - 1.0


def annualized_volatility(returns: List[float], periods_per_year: float = 252.0) -> float:
    """Annualized standard deviation of returns: sigma * sqrt(periods_per_year)."""
    if len(returns) < 2:
        return 0.0
    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std_dev = math.sqrt(max(0.0, variance))
    return std_dev * math.sqrt(periods_per_year)


def sharpe_ratio(
    returns: List[float],
    risk_free_rate: float = 0.04,
    periods_per_year: float = 252.0,
) -> float:
    """
    Annualized Sharpe Ratio: (Annualized Return - Rf) / Annualized Volatility.
    """
    if len(returns) < 2:
        return 0.0
    ann_vol = annualized_volatility(returns, periods_per_year)
    if ann_vol <= 1e-9:
        return 0.0
    mean_periodic = sum(returns) / len(returns)
    ann_return = mean_periodic * periods_per_year
    return (ann_return - risk_free_rate) / ann_vol


def sortino_ratio(
    returns: List[float],
    risk_free_rate: float = 0.04,
    target_return: float = 0.0,
    periods_per_year: float = 252.0,
) -> float:
    """
    Annualized Sortino Ratio: Excess return divided by annualized downside semi-deviation.
    Only penalizes negative volatility below the minimum acceptable return (target_return).
    """
    if len(returns) < 2:
        return 0.0
    downside_diffs = [min(0.0, r - (target_return / periods_per_year)) for r in returns]
    downside_variance = sum(d ** 2 for d in downside_diffs) / len(returns)
    downside_dev = math.sqrt(downside_variance) * math.sqrt(periods_per_year)

    if downside_dev <= 1e-9:
        # No downside deviations; return high score if positive return
        mean_periodic = sum(returns) / len(returns)
        return 5.0 if mean_periodic > 0 else 0.0

    mean_periodic = sum(returns) / len(returns)
    ann_return = mean_periodic * periods_per_year
    return (ann_return - risk_free_rate) / downside_dev


def max_drawdown(equity_curve: List[float]) -> Tuple[float, int, List[float]]:
    """
    Calculate Maximum Peak-to-Trough Drawdown (MDD) as a positive percentage (0.0 to 1.0).
    Returns (mdd_fraction, max_duration_bars, underwater_curve).
    """
    if len(equity_curve) < 2:
        return 0.0, 0, [0.0] * len(equity_curve)

    peak = equity_curve[0]
    mdd = 0.0
    underwater: List[float] = []
    duration = 0
    max_duration = 0

    for eq in equity_curve:
        if eq > peak:
            peak = eq
            duration = 0
        else:
            duration += 1
            if duration > max_duration:
                max_duration = duration

        dd = (peak - eq) / peak if peak > 0 else 0.0
        underwater.append(-dd)
        if dd > mdd:
            mdd = dd

    return mdd, max_duration, underwater


def calmar_ratio(cagr_val: float, mdd_val: float) -> float:
    """Calmar Ratio = CAGR / |Maximum Drawdown|."""
    if mdd_val <= 1e-6:
        return cagr_val / 0.001 if cagr_val > 0 else 0.0
    return cagr_val / mdd_val


def value_at_risk(
    returns: List[float],
    confidence: float = 0.95,
    method: str = "historical",
) -> float:
    """
    Periodic Value at Risk (VaR) returned as a positive percentage representing loss threshold.
    e.g. VaR 95% = 0.021 means 95% of bars experience losses no greater than 2.1%.
    """
    if not returns:
        return 0.0

    if method == "parametric":
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / max(1, len(returns) - 1)
        sigma = math.sqrt(max(0.0, variance))
        # Z-scores: 95% = 1.6449, 99% = 2.3263
        z = 2.3263 if confidence >= 0.985 else 1.6449
        var_val = -(mean_r - z * sigma)
        return max(0.0, var_val)
    else:
        # Historical / empirical quantile
        sorted_returns = sorted(returns)
        idx = max(0, int(math.floor((1.0 - confidence) * len(sorted_returns))))
        cutoff = sorted_returns[idx]
        return max(0.0, -cutoff)


def conditional_var(returns: List[float], confidence: float = 0.95) -> float:
    """
    Conditional Value at Risk (CVaR) / Expected Shortfall (ES).
    The expected loss on condition that the loss exceeds the VaR threshold (tail expectation).
    """
    if not returns:
        return 0.0
    sorted_returns = sorted(returns)
    idx = max(1, int(math.floor((1.0 - confidence) * len(sorted_returns))))
    tail = sorted_returns[:idx]
    if not tail:
        return 0.0
    mean_tail = sum(tail) / len(tail)
    return max(0.0, -mean_tail)


def beta_and_alpha(
    returns: List[float],
    benchmark_returns: List[float],
    risk_free_rate: float = 0.04,
    periods_per_year: float = 252.0,
) -> Tuple[float, float]:
    """
    Compute CAPM Beta and Jensen's Annualized Alpha against a benchmark (e.g. SPY).
    Beta = Cov(Rs, Rb) / Var(Rb)
    Alpha = (Rs_ann - Rf) - Beta * (Rb_ann - Rf)
    """
    n = min(len(returns), len(benchmark_returns))
    if n < 3:
        return 1.0, 0.0

    r_s = returns[:n]
    r_b = benchmark_returns[:n]

    mean_s = sum(r_s) / n
    mean_b = sum(r_b) / n

    cov = sum((s - mean_s) * (b - mean_b) for s, b in zip(r_s, r_b)) / (n - 1)
    var_b = sum((b - mean_b) ** 2 for b in r_b) / (n - 1)

    if var_b <= 1e-9:
        return 1.0, 0.0

    beta = cov / var_b

    ann_s = mean_s * periods_per_year
    ann_b = mean_b * periods_per_year
    alpha = (ann_s - risk_free_rate) - beta * (ann_b - risk_free_rate)

    return beta, alpha


def calculate_trade_statistics(trade_pnls: List[float]) -> Dict[str, Any]:
    """Compute comprehensive win/loss trade execution metrics."""
    if not trade_pnls:
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "payoff_ratio": 0.0,
            "average_trade_pnl": 0.0,
            "largest_winning_trade": 0.0,
            "largest_losing_trade": 0.0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
        }

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    total_trades = len(trade_pnls)
    win_count = len(wins)
    loss_count = len(losses)

    win_rate = (win_count / total_trades) * 100.0 if total_trades > 0 else 0.0

    total_gains = sum(wins)
    total_losses = abs(sum(losses))
    profit_factor = (total_gains / total_losses) if total_losses > 0 else (total_gains if total_gains > 0 else 1.0)

    avg_win = (total_gains / win_count) if win_count > 0 else 0.0
    avg_loss = (total_losses / loss_count) if loss_count > 0 else 0.0
    payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else (avg_win if avg_win > 0 else 1.0)

    # Max consecutive streaks
    cur_win_streak = 0
    max_win_streak = 0
    cur_loss_streak = 0
    max_loss_streak = 0

    for p in trade_pnls:
        if p > 0:
            cur_win_streak += 1
            cur_loss_streak = 0
            if cur_win_streak > max_win_streak:
                max_win_streak = cur_win_streak
        elif p < 0:
            cur_loss_streak += 1
            cur_win_streak = 0
            if cur_loss_streak > max_loss_streak:
                max_loss_streak = cur_loss_streak
        else:
            cur_win_streak = 0
            cur_loss_streak = 0

    return {
        "total_trades": total_trades,
        "winning_trades": win_count,
        "losing_trades": loss_count,
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff_ratio,
        "average_trade_pnl": sum(trade_pnls) / total_trades if total_trades > 0 else 0.0,
        "largest_winning_trade": max(wins) if wins else 0.0,
        "largest_losing_trade": min(losses) if losses else 0.0,
        "max_consecutive_wins": max_win_streak,
        "max_consecutive_losses": max_loss_streak,
    }


def generate_tear_sheet(
    equity_curve: List[float],
    benchmark_curve: Optional[List[float]] = None,
    trade_pnls: Optional[List[float]] = None,
    risk_free_rate: float = 0.04,
    periods_per_year: float = 252.0,
) -> TearSheetMetrics:
    """
    Construct complete Wall-Street standard quantitative tear sheet scorecard.
    """
    if not equity_curve:
        equity_curve = [100_000.0]

    init_cap = equity_curve[0]
    end_cap = equity_curve[-1]
    net_pnl = end_cap - init_cap
    cum_ret = (net_pnl / init_cap) * 100.0 if init_cap > 0 else 0.0

    returns = calculate_returns(equity_curve)
    cagr_val = cagr(equity_curve, periods_per_year) * 100.0
    vol_val = annualized_volatility(returns, periods_per_year) * 100.0
    sharpe = sharpe_ratio(returns, risk_free_rate, periods_per_year)
    sortino = sortino_ratio(returns, risk_free_rate, target_return=0.0, periods_per_year=periods_per_year)
    mdd, mdd_duration, _ = max_drawdown(equity_curve)
    calmar = calmar_ratio(cagr_val / 100.0, mdd)

    var95 = value_at_risk(returns, confidence=0.95, method="historical") * 100.0
    var99 = value_at_risk(returns, confidence=0.99, method="historical") * 100.0
    cvar95 = conditional_var(returns, confidence=0.95) * 100.0
    cvar99 = conditional_var(returns, confidence=0.99) * 100.0

    # Benchmark beta and alpha
    bmk_ret_pct = 0.0
    if benchmark_curve and len(benchmark_curve) >= 2:
        b_returns = calculate_returns(benchmark_curve)
        beta, alpha = beta_and_alpha(returns, b_returns, risk_free_rate, periods_per_year)
        alpha_pct = alpha * 100.0
        if benchmark_curve[0] > 0:
            bmk_ret_pct = ((benchmark_curve[-1] - benchmark_curve[0]) / benchmark_curve[0]) * 100.0
    else:
        beta = 1.0
        alpha_pct = 0.0

    trade_stats = calculate_trade_statistics(trade_pnls or [])

    return TearSheetMetrics(
        initial_capital=init_cap,
        ending_capital=end_cap,
        total_net_pnl=net_pnl,
        cumulative_return_pct=cum_ret,
        cagr_pct=cagr_val,
        annualized_volatility_pct=vol_val,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        max_drawdown_pct=mdd * 100.0,
        max_drawdown_duration_bars=mdd_duration,
        var_95_pct=var95,
        var_99_pct=var99,
        cvar_95_pct=cvar95,
        cvar_99_pct=cvar99,
        beta=beta,
        alpha_pct=alpha_pct,
        benchmark_return_pct=bmk_ret_pct,
        total_trades=trade_stats["total_trades"],
        winning_trades=trade_stats["winning_trades"],
        losing_trades=trade_stats["losing_trades"],
        win_rate_pct=trade_stats["win_rate_pct"],
        profit_factor=trade_stats["profit_factor"],
        payoff_ratio=trade_stats["payoff_ratio"],
        average_trade_pnl=trade_stats["average_trade_pnl"],
        largest_winning_trade=trade_stats["largest_winning_trade"],
        largest_losing_trade=trade_stats["largest_losing_trade"],
        max_consecutive_wins=trade_stats["max_consecutive_wins"],
        max_consecutive_losses=trade_stats["max_consecutive_losses"],
    )
