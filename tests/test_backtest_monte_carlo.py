"""
Unit and integration test suite for ChronosMatch Institutional Quantitative Backtesting,
Risk Metrics Tear Sheet, and Monte Carlo Engine.
"""

import math
import pytest

from chronos_match.algo.risk_metrics import (
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
    generate_tear_sheet,
    TearSheetMetrics,
)
from chronos_match.algo.monte_carlo import (
    MonteCarloEngine,
    MonteCarloResult,
)
from chronos_match.algo.backtester import (
    BacktestEngine,
    BacktestResult,
    BacktestTrade,
    DualEMABreakoutStrategy,
    MeanReversionBollingerStrategy,
    BayesianOrderFlowStrategy,
    RLTWAPExecutionBenchmark,
)
from chronos_match.market.data_loader import MarketDataLoader, StockCandle
from chronos_match.web.server import (
    BacktestRunRequest,
    MonteCarloRunRequest,
    api_get_backtest_strategies,
    api_run_backtest,
    api_run_monte_carlo,
)


def test_risk_metrics_vectorized_math():
    """Verify deterministic mathematical correctness of quant risk metrics."""
    # Test periodic return calculation
    prices = [100.0, 105.0, 102.0, 108.0, 110.0]
    rets = calculate_returns(prices)
    assert len(rets) == 4
    assert abs(rets[0] - 0.05) < 1e-6
    assert abs(rets[1] - (-0.0285714)) < 1e-4

    # Test CAGR
    # 100 to 200 in 252 bars (1 year) -> 100% CAGR
    daily_growth = [100.0 * (2.0 ** (i / 252.0)) for i in range(253)]
    cagr_computed = cagr(daily_growth, periods_per_year=252.0)
    assert abs(cagr_computed - 1.0) < 1e-3

    # Test Annualized Volatility
    # Fixed alternating returns +/- 1%
    test_rets = [0.01, -0.01] * 50
    vol = annualized_volatility(test_rets, periods_per_year=252.0)
    assert vol > 0.0
    assert abs(vol - (0.01 * math.sqrt(252.0))) < 0.02

    # Test Sharpe & Sortino Ratios
    # Returns with upside skew and moderate downside deviations
    mixed_rets = [0.01, -0.005, 0.015, -0.002, 0.008] * 40
    sr = sharpe_ratio(mixed_rets, risk_free_rate=0.04, periods_per_year=252.0)
    assert sr > 0.0

    sortino = sortino_ratio(mixed_rets, risk_free_rate=0.04, periods_per_year=252.0)
    assert sortino >= sr

    # Test Max Drawdown
    equity_curve = [100.0, 120.0, 90.0, 110.0, 80.0, 130.0]
    # Peak at 120.0, low at 80.0 -> DD = (120 - 80) / 120 = 40 / 120 = 33.33%
    mdd, dur, dd_curve = max_drawdown(equity_curve)
    assert abs(mdd - (40.0 / 120.0)) < 1e-4
    assert dur > 0
    assert len(dd_curve) == len(equity_curve)

    # Test Calmar Ratio
    calmar = calmar_ratio(cagr_val=0.30, mdd_val=0.15)
    assert abs(calmar - 2.0) < 1e-4

    # Test Value at Risk (VaR) and CVaR (Expected Shortfall)
    sample_returns = [-0.05, -0.04, -0.03, -0.02, -0.01, 0.00, 0.01, 0.02, 0.03, 0.04]
    var95 = value_at_risk(sample_returns, confidence=0.95, method="historical")
    assert var95 > 0.0  # VaR reported as positive loss percentage

    cvar95 = conditional_var(sample_returns, confidence=0.95)
    assert cvar95 >= var95  # CVaR is always >= VaR

    # Test Beta and Alpha vs Benchmark
    # Perfectly correlated 1.5x leveraged portfolio
    bmk_rets = [0.01, -0.01, 0.02, -0.015, 0.005] * 20
    strat_rets = [1.5 * r for r in bmk_rets]
    beta, alpha = beta_and_alpha(strat_rets, bmk_rets, risk_free_rate=0.0)
    assert abs(beta - 1.5) < 0.05
    assert abs(alpha) < 0.05

    # Test Trade Statistics
    trade_pnls = [150.0, 200.0, -80.0, -120.0, 300.0, -50.0]
    stats = calculate_trade_statistics(trade_pnls)
    assert stats["total_trades"] == 6
    assert stats["winning_trades"] == 3
    assert stats["losing_trades"] == 3
    assert abs(stats["win_rate_pct"] - 50.0) < 1e-4
    assert stats["profit_factor"] > 1.0


def test_monte_carlo_resampling_and_risk_projection():
    """Verify Monte Carlo forward cone and tail risk simulation."""
    mc = MonteCarloEngine(seed=42)
    sample_returns = [0.005, -0.003, 0.008, -0.004, 0.006, 0.001, -0.002, 0.01] * 20

    # 1. Bootstrap simulation
    res_boot = mc.simulate_from_returns(
        returns=sample_returns,
        initial_equity=100_000.0,
        forecast_bars=30,
        num_simulations=200,
        ruin_drawdown_pct=15.0,
        method="bootstrap",
    )
    assert isinstance(res_boot, MonteCarloResult)
    assert res_boot.num_simulations == 200
    assert res_boot.forecast_bars == 30
    assert len(res_boot.percentiles["p50"]) == 31
    assert res_boot.median_terminal_equity > 0.0
    assert 0.0 <= res_boot.probability_of_ruin_pct <= 100.0
    assert res_boot.terminal_5th_percentile <= res_boot.median_terminal_equity <= res_boot.terminal_95th_percentile

    # 2. Student-t Jump Diffusion (GBM) simulation
    res_gbm = mc.simulate_from_returns(
        returns=sample_returns,
        initial_equity=100_000.0,
        forecast_bars=30,
        num_simulations=200,
        ruin_drawdown_pct=15.0,
        method="gbm",
    )
    assert isinstance(res_gbm, MonteCarloResult)
    assert len(res_gbm.sample_trajectories) <= 30
    assert len(res_gbm.sample_trajectories) > 0
    assert res_gbm.median_terminal_equity > 0.0

    # Test to_dict serialization
    d = res_boot.to_dict()
    assert "percentile_fan" in d
    assert "probability_of_ruin" in d
    assert "median_terminal_equity" in d


def test_backtesting_strategies_and_order_flow():
    """Verify event-driven backtesting engine across all 4 quant alpha models."""
    data_loader = MarketDataLoader()
    engine = BacktestEngine(data_loader=data_loader)

    # 1. Catalog test
    catalog = engine.get_supported_strategies()
    assert len(catalog) >= 4
    strategy_ids = [s["id"] for s in catalog]
    assert "dual_ema" in strategy_ids
    assert "bollinger_reversion" in strategy_ids
    assert "bayesian_flow" in strategy_ids
    assert "twap_benchmark" in strategy_ids

    # 2. Run backtest with Dual EMA
    res_ema = engine.run_backtest(
        symbol="NVDA",
        strategy_id="dual_ema",
        initial_capital=100_000.0,
        max_bars=150,
        commission_bps=5.0,
        slippage_bps=3.5,
        stop_loss_pct=4.0,
        take_profit_pct=8.0,
        run_monte_carlo=True,
        monte_carlo_sims=100,
    )
    assert isinstance(res_ema, BacktestResult)
    assert len(res_ema.equity_curve) == 150
    assert len(res_ema.benchmark_curve) == 150
    assert res_ema.tear_sheet is not None
    assert res_ema.monte_carlo is not None
    assert "sharpe_ratio" in res_ema.tear_sheet.to_dict()

    # 3. Run backtest with Bollinger Mean Reversion
    res_bb = engine.run_backtest(
        symbol="AAPL",
        strategy_id="bollinger_reversion",
        initial_capital=100_000.0,
        max_bars=150,
        commission_bps=5.0,
        slippage_bps=3.5,
        run_monte_carlo=False,
    )
    assert isinstance(res_bb, BacktestResult)
    assert len(res_bb.equity_curve) == 150
    assert res_bb.tear_sheet.cumulative_return_pct is not None

    # 4. Run backtest with Bayesian Order Flow Momentum
    res_flow = engine.run_backtest(
        symbol="NVDA",
        strategy_id="bayesian_flow",
        initial_capital=100_000.0,
        max_bars=150,
        run_monte_carlo=False,
    )
    assert isinstance(res_flow, BacktestResult)
    assert len(res_flow.equity_curve) == 150

    # 5. Run backtest with RL-TWAP Execution Benchmark
    res_twap = engine.run_backtest(
        symbol="AAPL",
        strategy_id="twap_benchmark",
        initial_capital=100_000.0,
        max_bars=100,
        run_monte_carlo=False,
    )
    assert isinstance(res_twap, BacktestResult)
    assert len(res_twap.trades) > 0  # Systematic TWAP trades periodically


def test_fastapi_quant_backtest_and_monte_carlo_endpoints():
    """Verify REST API handlers for Backtesting and Monte Carlo simulation."""
    # 1. GET /api/backtest/strategies
    strats = api_get_backtest_strategies()
    assert isinstance(strats, list)
    assert len(strats) >= 4

    # 2. POST /api/backtest/run
    req = BacktestRunRequest(
        symbol="NVDA",
        strategy="dual_ema",
        initial_capital=100_000.0,
        max_bars=120,
        commission_bps=5.0,
        slippage_bps=3.5,
        stop_loss_pct=4.0,
        take_profit_pct=8.0,
        run_monte_carlo=True,
        monte_carlo_sims=150,
    )
    result = api_run_backtest(req)
    assert "tear_sheet" in result
    assert "equity_curve" in result
    assert "monte_carlo" in result
    assert result["initial_capital"] == 100_000.0
    assert "sharpe_ratio" in result["tear_sheet"]
    assert "probability_of_ruin" in result["monte_carlo"]

    # 3. POST /api/monte-carlo/simulate
    mc_req = MonteCarloRunRequest(
        symbol="NVDA",
        num_simulations=200,
        forecast_bars=30,
        initial_equity=100_000.0,
        ruin_drawdown_pct=20.0,
        method="bootstrap",
    )
    mc_result = api_run_monte_carlo(mc_req)
    assert "median_terminal_equity" in mc_result
    assert "percentile_fan" in mc_result
    assert mc_result["num_simulations"] == 200
    assert len(mc_result["percentile_fan"]["p50"]) == 31
