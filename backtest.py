#!/usr/bin/env python3
"""
FluxCore: Institutional Quantitative Strategy Backtester & Bloomberg Tear Sheet CLI.

Executes quantitative alpha strategies against historical market data with:
- Realistic execution slippage and exchange fee modeling
- Multi-metric Bloomberg PORT / PyFolio risk scorecard (Sharpe, Sortino, Calmar, MDD, VaR, CVaR)
- Forward Monte Carlo stress simulation (Student-t + block bootstrapping)
- High-fidelity Unicode terminal equity curve
- CSV trade log export for external quantitative audit

Usage:
    python backtest.py --symbol NVDA --strategy dual_ema
    python backtest.py --symbol AAPL --strategy bollinger_reversion --monte-carlo
    python backtest.py --symbol BTC-USD --strategy bayesian_flow --capital 250000 --export-csv trades.csv
"""

import argparse
import csv
import math
import os
import sys
import time
from typing import List, Tuple, Optional

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from fluxcore.algo.backtester import BacktestEngine, BacktestResult, BacktestTrade
from fluxcore.algo.risk_metrics import TearSheetMetrics
from fluxcore.algo.monte_carlo import MonteCarloResult


console = Console()


def render_unicode_chart(
    series: List[float],
    width: int = 60,
    height: int = 7,
    color_up: str = "bright_green",
    color_down: str = "bright_red",
) -> List[Text]:
    """
    Renders an institutional multi-row ASCII / Unicode area and line chart
    in terminal with Y-axis price labels.
    """
    if not series or len(series) < 2:
        return [Text("Insufficient data for equity curve.", style="dim")]

    # Subsample series to width
    n = len(series)
    if n > width:
        step = (n - 1) / (width - 1)
        sampled = [series[int(round(i * step))] for i in range(width)]
    elif n < width:
        # Interpolate
        sampled = []
        for i in range(width):
            idx_float = i * (n - 1) / (width - 1)
            i0 = int(math.floor(idx_float))
            i1 = min(n - 1, i0 + 1)
            frac = idx_float - i0
            val = series[i0] * (1.0 - frac) + series[i1] * frac
            sampled.append(val)
    else:
        sampled = list(series)

    min_val = min(sampled)
    max_val = max(sampled)
    val_range = max(1e-6, max_val - min_val)

    # 8-level vertical block characters
    blocks = [" ", " ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]

    lines: List[Text] = []
    is_profitable = sampled[-1] >= sampled[0]
    line_style = color_up if is_profitable else color_down

    for row in range(height - 1, -1, -1):
        # Y-axis price label for this row
        row_val = min_val + (row / max(1, height - 1)) * val_range
        label_str = f"${row_val:>9,.0f} ┤"

        txt = Text(label_str, style="dim cyan")
        for col_val in sampled:
            norm = (col_val - min_val) / val_range  # 0.0 to 1.0
            col_level = norm * height
            row_diff = col_level - row

            if row_diff >= 1.0:
                txt.append("█", style=line_style)
            elif row_diff > 0.0:
                b_idx = int(round(row_diff * 8))
                b_idx = max(1, min(8, b_idx))
                txt.append(blocks[b_idx], style=line_style)
            else:
                txt.append(" ", style="dim")
        lines.append(txt)

    # Bottom axis line
    axis_txt = Text(" " * 11 + "└" + "─" * len(sampled), style="dim")
    lines.append(axis_txt)

    return lines


def build_tear_sheet_panels(result: BacktestResult, mc: Optional[MonteCarloResult] = None) -> List[Panel]:
    """Formats institutional performance, risk, and tail statistics."""
    ts: TearSheetMetrics = result.tear_sheet

    # --- Table 1: Returns & Capital Performance ---
    t_ret = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1), show_header=True)
    t_ret.add_column("Return & Factor Metrics", style="bold cyan")
    t_ret.add_column("Value", justify="right", style="bold white")

    t_ret.add_row("Initial Capital", f"${ts.initial_capital:,.2f}")
    t_ret.add_row("Ending Capital", f"${ts.ending_capital:,.2f}")

    pnl_style = "bold green" if ts.total_net_pnl >= 0 else "bold red"
    sign = "+" if ts.total_net_pnl >= 0 else ""
    t_ret.add_row("Total Net PnL", f"[{pnl_style}]{sign}${ts.total_net_pnl:,.2f}[/]")

    ret_style = "bold green" if ts.cumulative_return_pct >= 0 else "bold red"
    t_ret.add_row("Cumulative Return", f"[{ret_style}]{sign}{ts.cumulative_return_pct:.2f}%[/]")
    t_ret.add_row("CAGR (Annualized)", f"{ts.cagr_pct:.2f}%")
    t_ret.add_row("Annualized Volatility (σ)", f"{ts.annualized_volatility_pct:.2f}%")

    bm_sign = "+" if ts.benchmark_return_pct >= 0 else ""
    t_ret.add_row("Benchmark Return (Buy&Hold)", f"{bm_sign}{ts.benchmark_return_pct:.2f}%")

    alpha_style = "green" if ts.alpha_pct >= 0 else "red"
    a_sign = "+" if ts.alpha_pct >= 0 else ""
    t_ret.add_row("Jensen's Alpha", f"[{alpha_style}]{a_sign}{ts.alpha_pct:.2f}%[/]")
    t_ret.add_row("Beta (vs Benchmark)", f"{ts.beta:.2f}")

    # --- Table 2: Risk-Adjusted Ratios & Trades ---
    t_trades = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1), show_header=True)
    t_trades.add_column("Risk-Adjusted & Trade Stats", style="bold yellow")
    t_trades.add_column("Value", justify="right", style="bold white")

    # Sharpe ratio coloring
    sharpe_style = "bold green" if ts.sharpe_ratio >= 1.5 else ("bold yellow" if ts.sharpe_ratio >= 0.8 else "bold red")
    t_trades.add_row("Sharpe Ratio (Rf=2.0%)", f"[{sharpe_style}]{ts.sharpe_ratio:.2f}[/]")
    t_trades.add_row("Sortino Ratio (Downside σ)", f"{ts.sortino_ratio:.2f}")
    t_trades.add_row("Calmar Ratio (Ret / MDD)", f"{ts.calmar_ratio:.2f}")

    t_trades.add_row("Total Trades Executed", f"{ts.total_trades}")
    win_style = "green" if ts.win_rate_pct >= 50.0 else "yellow"
    t_trades.add_row("Win Rate %", f"[{win_style}]{ts.win_rate_pct:.1f}% ({ts.winning_trades}W / {ts.losing_trades}L)[/]")

    pf_style = "green" if ts.profit_factor >= 1.5 else ("yellow" if ts.profit_factor >= 1.0 else "red")
    pf_str = "∞" if ts.profit_factor > 99.0 else f"{ts.profit_factor:.2f}"
    t_trades.add_row("Profit Factor", f"[{pf_style}]{pf_str}[/]")
    t_trades.add_row("Payoff Ratio (Avg W / Avg L)", f"{ts.payoff_ratio:.2f}")

    avg_style = "green" if ts.average_trade_pnl >= 0 else "red"
    t_trades.add_row("Avg Trade PnL", f"[{avg_style}]${ts.average_trade_pnl:,.2f}[/]")
    t_trades.add_row("Max Consecutive W / L", f"{ts.max_consecutive_wins} wins / {ts.max_consecutive_losses} losses")

    # --- Table 3: Downside Tail-Risk Metrics ---
    t_risk = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1), show_header=True)
    t_risk.add_column("Downside Tail-Risk Guardrails", style="bold red")
    t_risk.add_column("Value", justify="right", style="bold white")

    mdd_style = "bold green" if ts.max_drawdown_pct < 10.0 else ("bold yellow" if ts.max_drawdown_pct < 20.0 else "bold red")
    t_risk.add_row("Max Drawdown (MDD)", f"[{mdd_style}]-{ts.max_drawdown_pct:.2f}%[/]")
    t_risk.add_row("MDD Peak-to-Trough", f"{ts.max_drawdown_duration_bars} bars")
    t_risk.add_row("Daily VaR (95% Confidence)", f"-{ts.var_95_pct:.2f}%")
    t_risk.add_row("Daily VaR (99% Extreme Tail)", f"-{ts.var_99_pct:.2f}%")
    t_risk.add_row("CVaR / Expected Shortfall (95%)", f"-{ts.cvar_95_pct:.2f}%")
    t_risk.add_row("CVaR / Expected Shortfall (99%)", f"-{ts.cvar_99_pct:.2f}%")
    t_risk.add_row("Largest Winning Trade", f"[green]+${ts.largest_winning_trade:,.2f}[/]")
    t_risk.add_row("Largest Losing Trade", f"[red]-${abs(ts.largest_losing_trade):,.2f}[/]")

    p_ret = Panel(t_ret, border_style="cyan", title="[bold cyan]1. RETURN & PORTFOLIO METRICS[/]", box=box.ROUNDED)
    p_trades = Panel(t_trades, border_style="yellow", title="[bold yellow]2. RISK-ADJUSTED & TRADE EXECUTION[/]", box=box.ROUNDED)
    p_risk = Panel(t_risk, border_style="red", title="[bold red]3. DOWNSIDE TAIL RISK (VaR / CVaR)[/]", box=box.ROUNDED)

    return [p_ret, p_trades, p_risk]


def build_monte_carlo_panel(mc: MonteCarloResult) -> Panel:
    """Constructs Monte Carlo tail risk forecasting panel."""
    t_mc = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1))
    t_mc.add_column("Monte Carlo Metric (1,000 Paths, Student-t)", style="bold magenta")
    t_mc.add_column("Simulation Value", justify="right", style="bold white")

    t_mc.add_row("Simulated Forward Trajectories", f"{mc.num_simulations:,} paths")
    t_mc.add_row("Forecast Horizon", f"{mc.forecast_bars} trading bars")
    t_mc.add_row("Initial Capital Baseline", f"${mc.initial_equity:,.2f}")

    med_diff = mc.median_terminal_equity - mc.initial_equity
    med_style = "bold green" if med_diff >= 0 else "bold red"
    med_sign = "+" if med_diff >= 0 else ""
    t_mc.add_row("Median Projected Capital", f"[{med_style}]${mc.median_terminal_equity:,.2f} ({med_sign}{(med_diff / mc.initial_equity)*100:.1f}%)[/]")

    t_mc.add_row("95% Confidence Upper Bound", f"[green]${mc.terminal_95th_percentile:,.2f}[/]")
    t_mc.add_row("5% Tail-Risk Stress Lower Bound", f"[red]${mc.terminal_5th_percentile:,.2f}[/]")

    ruin_style = "bold green" if mc.probability_of_ruin_pct <= 5.0 else ("bold yellow" if mc.probability_of_ruin_pct <= 15.0 else "bold red")
    t_mc.add_row("Probability of Ruin (20%+ Drawdown)", f"[{ruin_style}]{mc.probability_of_ruin_pct:.1f}%[/]")

    prob_prof_style = "bold green" if mc.probability_of_profit_pct >= 60.0 else "bold yellow"
    t_mc.add_row("Probability of Net Profit", f"[{prob_prof_style}]{mc.probability_of_profit_pct:.1f}%[/]")
    t_mc.add_row("Worst 95th Pct Drawdown", f"-{mc.worst_drawdown_95th_pct:.2f}%")
    t_mc.add_row("Forward Terminal CVaR (95%)", f"-{mc.terminal_cvar_95_pct:.2f}%")

    return Panel(
        t_mc,
        border_style="magenta",
        title="[bold magenta]4. MONTE CARLO STRESS TEST & RUIN PROBABILITY FORECAST[/]",
        box=box.ROUNDED,
    )


def build_trades_table(trades: List[BacktestTrade], max_rows: int = 8) -> Panel:
    """Builds a formatted summary table of recent completed trades that fits inside 80 columns."""
    table = Table(box=box.SIMPLE_HEAD, expand=False, padding=(0, 1), show_header=True)
    table.add_column("#", justify="center", style="dim", no_wrap=True)
    table.add_column("Entry Date", justify="center", no_wrap=True)
    table.add_column("Exit Date", justify="center", no_wrap=True)
    table.add_column("Entry Px", justify="right", no_wrap=True)
    table.add_column("Exit Px", justify="right", no_wrap=True)
    table.add_column("Net PnL", justify="right", no_wrap=True)
    table.add_column("Return", justify="right", no_wrap=True)
    table.add_column("Exit Trigger", justify="left", no_wrap=True)

    display_trades = trades[-max_rows:] if len(trades) > max_rows else trades

    if not display_trades:
        table.add_row("-", "-", "-", "-", "-", "$0.00", "0.0%", "NO_TRADES")
    else:
        for t in display_trades:
            pnl_style = "bold green" if t.net_pnl >= 0 else "bold red"
            sign = "+" if t.net_pnl >= 0 else ""

            reason_style = "dim"
            if t.exit_reason == "TAKE_PROFIT":
                reason_style = "bold green"
            elif t.exit_reason == "STOP_LOSS":
                reason_style = "bold red"
            elif t.exit_reason == "SIGNAL":
                reason_style = "cyan"

            table.add_row(
                str(t.trade_id),
                t.entry_date[:10],
                t.exit_date[:10],
                f"${t.entry_price:,.2f}",
                f"${t.exit_price:,.2f}",
                f"[{pnl_style}]{sign}${t.net_pnl:,.2f}[/]",
                f"[{pnl_style}]{sign}{t.return_pct:.2f}%[/]",
                f"[{reason_style}]{t.exit_reason}[/]",
            )

    title = f"[bold white]RECENT TRADE EXECUTIONS (Showing last {len(display_trades)} of {len(trades)} total)[/]"
    return Panel(table, border_style="cyan", title=title, box=box.ROUNDED)


def export_trades_csv(trades: List[BacktestTrade], filepath: str, result: BacktestResult):
    """Exports executed trade records to a clean CSV file."""
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["# FluxCore Quantitative Backtest Trade Log"])
        writer.writerow(["# Symbol", result.symbol])
        writer.writerow(["# Strategy", result.strategy_name])
        writer.writerow(["# Total Net PnL", f"{result.total_net_pnl:.2f}"])
        writer.writerow(["# Cumulative Return %", f"{result.tear_sheet.cumulative_return_pct:.2f}"])
        writer.writerow([])
        writer.writerow([
            "TradeID", "Symbol", "Side", "EntryDate", "ExitDate",
            "EntryPrice", "ExitPrice", "Quantity", "GrossPnL",
            "Commission", "Slippage", "NetPnL", "ReturnPct",
            "HoldingBars", "ExitReason"
        ])
        for t in trades:
            writer.writerow([
                t.trade_id, t.symbol, t.side, t.entry_date, t.exit_date,
                f"{t.entry_price:.2f}", f"{t.exit_price:.2f}", t.quantity,
                f"{t.gross_pnl:.2f}", f"{t.commission:.2f}", f"{t.slippage:.2f}",
                f"{t.net_pnl:.2f}", f"{t.return_pct:.2f}",
                t.holding_bars, t.exit_reason
            ])


def main():
    parser = argparse.ArgumentParser(
        description="FluxCore: Institutional Quantitative Strategy Backtester & Bloomberg PORT Tear Sheet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Available Quantitative Strategies:
  dual_ema             Dual EMA Trend Breakout (12/26 EMA + RSI 45)
  bollinger_reversion  Bollinger Bands Mean Reversion (20-bar 2.0σ + RSI)
  bayesian_flow        Bayesian Microstructural Order Flow Momentum
  twap_benchmark       Systematic TWAP Benchmark (deterministic slicing)

Examples:
  python backtest.py --symbol NVDA --strategy dual_ema
  python backtest.py --symbol AAPL --strategy bollinger_reversion --monte-carlo
  python backtest.py --symbol BTC-USD --strategy bayesian_flow --capital 250000 --export-csv trades.csv
        """,
    )
    parser.add_argument("--symbol", type=str, default="NVDA", help="Asset ticker symbol (e.g. NVDA, AAPL, MSFT, BTC-USD, RELIANCE)")
    parser.add_argument(
        "--strategy",
        type=str,
        default="dual_ema",
        choices=["dual_ema", "bollinger_reversion", "bayesian_flow", "twap_benchmark"],
        help="Quantitative trading strategy",
    )
    parser.add_argument("--capital", type=float, default=100_000.0, help="Initial portfolio cash in USD (default: 100,000)")
    parser.add_argument("--bars", type=int, default=300, help="Maximum historical market bars to evaluate (default: 300)")
    parser.add_argument("--commission-bps", type=float, default=5.0, help="Exchange transaction fee in basis points (default: 5.0 bps = 0.05%%)")
    parser.add_argument("--slippage-bps", type=float, default=2.0, help="Execution slippage in basis points (default: 2.0 bps = 0.02%%)")
    parser.add_argument("--stop-loss", type=float, default=5.0, help="Stop loss risk threshold percentage (default: 5.0%%)")
    parser.add_argument("--take-profit", type=float, default=8.0, help="Take profit threshold percentage (default: 8.0%%)")
    parser.add_argument("--monte-carlo", action="store_true", help="Execute 1,000-path forward Monte Carlo tail risk stress test")
    parser.add_argument("--export-csv", type=str, default=None, help="Filepath to export trade log in CSV format")

    args = parser.parse_args()

    # Header Banner
    console.print()
    banner = Text()
    banner.append("FLUX", style="bold cyan")
    banner.append("CORE", style="bold white")
    banner.append("  |  INSTITUTIONAL QUANTITATIVE BACKTESTER & BLOOMBERG PORT TEAR SHEET\n", style="bold yellow")
    banner.append("Zero-Copy HFT Matching Engine  *  Vectorized Risk Scorecards  *  Monte Carlo Stress Testing", style="dim")

    console.print(Panel(banner, border_style="cyan", box=box.DOUBLE))

    # Initialize Engine & Run Backtest
    with console.status(f"[bold cyan]Loading {args.symbol.upper()} historical bars & executing {args.strategy}...[/]", spinner="dots"):
        t0 = time.perf_counter()
        engine = BacktestEngine()
        result = engine.run_backtest(
            symbol=args.symbol,
            strategy_id=args.strategy,
            initial_capital=args.capital,
            max_bars=args.bars,
            commission_bps=args.commission_bps,
            slippage_bps=args.slippage_bps,
            stop_loss_pct=args.stop_loss,
            take_profit_pct=args.take_profit,
            run_monte_carlo=args.monte_carlo,
            monte_carlo_sims=1_000,
        )
        elapsed_sec = time.perf_counter() - t0

    # Strategy Metadata Panel
    start_date = result.dates[0] if result.dates else "N/A"
    end_date = result.dates[-1] if result.dates else "N/A"
    meta_table = Table(box=box.MINIMAL, expand=True, show_header=False)
    meta_table.add_column("Param", style="dim cyan", width=18)
    meta_table.add_column("Value", style="bold white")
    meta_table.add_column("Param", style="dim cyan", width=18)
    meta_table.add_column("Value", style="bold white")

    meta_table.add_row("Symbol", f"[bold cyan]{result.symbol}[/]", "Strategy", f"[bold yellow]{result.strategy_name}[/]")
    meta_table.add_row("Historical Period", f"{start_date} to {end_date}", "Bars Evaluated", f"{len(result.dates):,} bars")
    meta_table.add_row("Commission / Slippage", f"{args.commission_bps:.1f} bps / {args.slippage_bps:.1f} bps", "Simulation Time", f"{elapsed_sec*1000.0:.2f} ms")

    console.print(Panel(meta_table, title="[bold white]SIMULATION SCOPE & CONFIGURATION[/]", border_style="blue", box=box.ROUNDED))

    # 3 Tear Sheet Panels
    p_ret, p_trades, p_risk = build_tear_sheet_panels(result, result.monte_carlo)
    console.print(p_ret)
    console.print(p_trades)
    console.print(p_risk)

    # Terminal Equity Curve Chart
    if result.equity_curve:
        chart_w = min(64, max(40, (console.width or 80) - 20))
        chart_lines = render_unicode_chart(result.equity_curve, width=chart_w, height=7)
        timeline_str = f"Start: {start_date} " + " " * max(2, chart_w - 24) + f"End: {end_date}"
        chart_group = Group(
            Text(f"Equity Curve Trajectory: ${result.initial_capital:,.0f} -> ${result.ending_capital:,.0f}", style="bold cyan"),
            *chart_lines,
            Text(timeline_str, style="dim"),
        )
        console.print(Panel(chart_group, title="[bold green]PORTFOLIO EQUITY TRAJECTORY (TERMINAL CHART)[/]", border_style="green", box=box.ROUNDED))

    # Monte Carlo Panel (if requested)
    if result.monte_carlo:
        console.print(build_monte_carlo_panel(result.monte_carlo))

    # Recent Executed Trades
    console.print(build_trades_table(result.trades, max_rows=8))

    # Export CSV if requested
    if args.export_csv:
        export_trades_csv(result.trades, args.export_csv, result)
        console.print(f"[bold green]✔ Trade execution log successfully exported to:[/] [cyan]{os.path.abspath(args.export_csv)}[/]")

    # Terminal Summary Footer
    sign = "+" if result.total_net_pnl >= 0 else ""
    summary_color = "bold green" if result.total_net_pnl >= 0 else "bold red"
    console.print(
        f"[{summary_color}]Backtest Completed: Net PnL {sign}${result.total_net_pnl:,.2f} "
        f"({sign}{result.tear_sheet.cumulative_return_pct:.2f}%) | Sharpe: {result.tear_sheet.sharpe_ratio:.2f} | "
        f"MDD: -{result.tear_sheet.max_drawdown_pct:.2f}%[/]\n"
    )


if __name__ == "__main__":
    main()
