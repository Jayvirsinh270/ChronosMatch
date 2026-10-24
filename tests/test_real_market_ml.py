"""
ChronosMatch: Test Suite for Phase 6 (Real Market Data, ML Impact Engine & Quantitative Algo Suite).

Validates:
1. Real historical stock & ETF data discovery, indexing, and candle parsing.
2. Market volatility, volume, and tick size profiling across US & Indian equities.
3. Adaptive ML Market Impact Engine: price reaction to live buy/sell order flow.
4. Almgren-Chriss / Kyle's Lambda price sensitivity and transient spread recovery.
5. Avellaneda-Stoikov Quantitative Market Maker inventory risk quoting.
6. Momentum & Order Flow Breakout bot execution and PnL accounting.
7. Historical Market Replayer micro-tick synthesis and Level 2 book interaction.
8. Web trading server real-market company selection and auto-trading endpoints.
"""

import math
import os
import time
import pytest

from chronos_match.market.data_loader import MarketDataLoader, StockCandle
from chronos_match.market.ml_impact_engine import MLMarketImpactEngine
from chronos_match.algo.market_maker import AvellanedaStoikovMarketMaker
from chronos_match.algo.momentum_bot import MomentumBreakoutBot
from chronos_match.algo import AlgoTradingManager
from chronos_match.market.router import MultiBookRouter, SymbolInfo
from chronos_match.market.replayer import HistoricalMarketReplayer
from chronos_match.web.server import WebTradingServer
from chronos_match.protocol import to_fixed_price, from_fixed_price, OrderSide


# ─────────────────────────────────────────────────────────────
# 1. REAL MARKET DATA LOADER & PROFILING TESTS
# ─────────────────────────────────────────────────────────────

def test_data_loader_discovery_and_profile():
    """Verify scanning and loading of real historical stock datasets."""
    loader = MarketDataLoader(data_root="data")
    symbols = loader.get_supported_tickers()
    assert len(symbols) > 5000, f"Expected >5000 indexed symbols, got {len(symbols)}"

    # Verify major US tech & Indian stocks are indexed
    assert "NVDA" in symbols
    assert "AAPL" in symbols
    assert "MSFT" in symbols
    assert "RELIANCE" in symbols or "500325" in symbols

    # Load candles for Apple
    aapl_candles = loader.load_candles("AAPL", max_bars=100)
    assert len(aapl_candles) == 100
    assert aapl_candles[-1].close > 0
    assert aapl_candles[-1].high >= aapl_candles[-1].low
    assert aapl_candles[-1].volume > 0

    # Compute historical profile
    nvda_profile = loader.compute_profile("NVDA", lookback=60)
    assert nvda_profile["ticker"] == "NVDA"
    assert nvda_profile["latest_price"] > 0
    assert nvda_profile["volatility_daily"] > 0
    assert nvda_profile["avg_volume"] > 0


def test_popular_companies_directory():
    """Verify catalog of curated popular companies."""
    loader = MarketDataLoader(data_root="data")
    popular = loader.get_popular_companies()
    assert len(popular) >= 5
    tickers = [p["ticker"] for p in popular]
    assert "NVDA" in tickers
    assert "AAPL" in tickers


# ─────────────────────────────────────────────────────────────
# 2. ADAPTIVE ML MARKET IMPACT ENGINE TESTS
# ─────────────────────────────────────────────────────────────

def test_ml_impact_engine_buy_and_sell_reaction():
    """Verify that market moves UP on aggressive buys and DOWN on aggressive sells."""
    engine = MLMarketImpactEngine(
        base_price=100.00,
        daily_volatility=0.02,
        avg_daily_volume=1_000_000,
    )

    # Initial state
    assert engine.get_effective_price() == 100.00

    # 1. Ingest aggressive BUY order: 20,000 shares
    delta_p = engine.record_execution(side=0, quantity=20_000, execution_price=100.00)
    assert delta_p > 0, "Buy order must produce positive price delta"

    price_after_buy = engine.get_effective_price()
    assert price_after_buy > 100.00, f"Price should move up after buy, got {price_after_buy}"

    telemetry = engine.get_telemetry()
    assert telemetry.order_flow_imbalance > 0
    assert telemetry.net_order_flow > 0
    assert telemetry.spread_multiplier > 1.0  # Spread widened after taking liquidity

    # 2. Ingest aggressive SELL order: 40,000 shares
    delta_p_sell = engine.record_execution(side=1, quantity=40_000, execution_price=price_after_buy)
    assert delta_p_sell < 0, "Sell order must produce negative price delta"

    price_after_sell = engine.get_effective_price()
    assert price_after_sell < price_after_buy, "Price should move down after heavy selling"


def test_ml_impact_transient_decay():
    """Verify that transient market impact and spread elevation decay over time."""
    engine = MLMarketImpactEngine(
        base_price=150.00,
        decay_half_life_sec=0.1,  # Fast decay for test
    )

    engine.record_execution(side=0, quantity=50_000, execution_price=150.00)
    high_impact_price = engine.get_effective_price()
    high_spread = engine.get_spread_multiplier()

    assert high_impact_price > 150.00
    assert high_spread > 1.0

    # Simulate elapsed time
    time.sleep(0.25)

    decayed_price = engine.get_effective_price()
    decayed_spread = engine.get_spread_multiplier()

    # Price and spread should recover toward baseline
    assert decayed_price < high_impact_price
    assert decayed_spread < high_spread


# ─────────────────────────────────────────────────────────────
# 3. AVELLANEDA-STOIKOV MARKET MAKER TESTS
# ─────────────────────────────────────────────────────────────

def test_avellaneda_stoikov_inventory_skew():
    """Verify that market maker skews quotes based on inventory holding."""
    mm = AvellanedaStoikovMarketMaker(
        bot_id=801,
        symbol="NVDA",
        risk_aversion=0.01,
    )

    mid = 150.00
    vol = 0.02

    # 1. Neutral Inventory (q = 0)
    r_neutral, bid_neutral, ask_neutral = mm.compute_quotes(mid_price=mid, volatility=vol)
    assert r_neutral == mid
    assert bid_neutral < mid < ask_neutral

    # 2. Long Inventory (q = +500 shares) -> Must skew reservation price DOWN
    mm.inventory = 500
    r_long, bid_long, ask_long = mm.compute_quotes(mid_price=mid, volatility=vol)
    assert r_long < mid, "Long inventory must skew reservation price downward to sell"
    assert bid_long < bid_neutral
    assert ask_long < ask_neutral

    # 3. Short Inventory (q = -500 shares) -> Must skew reservation price UP
    mm.inventory = -500
    r_short, bid_short, ask_short = mm.compute_quotes(mid_price=mid, volatility=vol)
    assert r_short > mid, "Short inventory must skew reservation price upward to buy"
    assert bid_short > bid_neutral
    assert ask_short > ask_neutral


def test_market_maker_fill_and_pnl_accounting():
    """Verify that quote executions update inventory, cash balance, and spread capture."""
    mm = AvellanedaStoikovMarketMaker(bot_id=801, symbol="NVDA")

    # Buy 100 shares @ $149.50
    mm.last_bid_price = 149.50
    mm.last_ask_price = 150.50
    mm.on_fill(side=0, price=149.50, quantity=100)
    assert mm.inventory == 100
    assert mm.cash_balance == -14950.0

    # Sell 100 shares @ $150.50 (Roundtrip spread capture)
    mm.on_fill(side=1, price=150.50, quantity=100)
    assert mm.inventory == 0
    assert mm.cash_balance == 100.0  # Captured $1.00 spread * 100 shares = +$100.00

    stats = mm.get_stats(current_mid=150.00)
    assert stats.total_pnl == 100.0
    assert stats.total_fills == 2


# ─────────────────────────────────────────────────────────────
# 4. MOMENTUM BREAKOUT BOT TESTS
# ─────────────────────────────────────────────────────────────

def test_momentum_bot_breakout_signals():
    """Verify momentum bot trend detection and profit taking."""
    bot = MomentumBreakoutBot(bot_id=802, symbol="NVDA", fast_period=3, slow_period=7, trade_size=50)
    bot.active = True

    # 1. Establish baseline
    bot.update_tick(100.00, order_flow_imbalance=0.0)

    # 2. Bullish breakout with order flow imbalance
    sig = bot.update_tick(101.50, order_flow_imbalance=0.30)
    assert sig is not None
    action, qty, px = sig
    assert action == "BUY"
    assert qty == 50
    assert bot.position == 50

    # 3. Push price higher to hit profit target (+1.5%)
    exit_sig = bot.update_tick(104.50, order_flow_imbalance=0.10)
    assert exit_sig is not None
    action_exit, qty_exit, px_exit = exit_sig
    assert action_exit == "SELL"
    assert bot.position == 0
    assert bot.winning_trades >= 1


# ─────────────────────────────────────────────────────────────
# 5. HISTORICAL REPLAYER & VENUE BINDING TESTS
# ─────────────────────────────────────────────────────────────

def test_historical_replayer_bind_and_step():
    """Verify replayer dynamically binds real stock and synthesizes order flow."""
    router = MultiBookRouter()
    replayer = HistoricalMarketReplayer(router=router, default_ticker="NVDA")

    venue = router.get_venue("NVDA")
    assert venue.info.ticker == "NVDA"
    assert venue.info.initial_price > 0

    # Turn on Market Maker and step ticks
    algo_mgr = replayer.algo_managers["NVDA"]
    algo_mgr.toggle_bot("mm", enable=True)

    initial_orders = venue.total_orders
    events = replayer.step_market_tick("NVDA")

    assert venue.total_orders > initial_orders
    assert replayer.impact_engines["NVDA"] is not None

    # Verify candlestick loading
    candles = replayer.get_candlesticks("NVDA", limit=50)
    assert len(candles) > 0
    assert "close" in candles[-1]


# ─────────────────────────────────────────────────────────────
# 6. WEB SERVER REAL-MARKET ENDPOINTS TESTS
# ─────────────────────────────────────────────────────────────

def test_web_server_real_market_endpoints():
    """Verify web server company selection, candles, and auto-trading endpoints."""
    server = WebTradingServer(enable_wal=False)

    # 1. Popular companies directory
    comps = server.replayer.data_loader.get_popular_companies()
    assert len(comps) >= 5

    # 2. Select real company (NVDA)
    venue = server.replayer.bind_stock("NVDA")
    assert "NVDA" in venue.info.ticker

    # 3. Retrieve historical candles
    candles = server.replayer.get_candlesticks("NVDA", limit=20)
    assert len(candles) == 20

    # 4. Toggle auto-trading bots
    algo_mgr = server.replayer.algo_managers["NVDA"]
    mm_active = algo_mgr.toggle_bot("mm", enable=True)
    mom_active = algo_mgr.toggle_bot("momentum", enable=True)
    assert mm_active is True
    assert mom_active is True

    # 5. Retrieve algo stats
    summary = algo_mgr.get_summary(venue.last_trade_price)
    assert "market_maker" in summary
    assert "momentum_bot" in summary

    # 6. Verify ML Impact reflects in snapshot
    snap = server.get_snapshot("NVDA")
    assert snap["ml_impact"] is not None
    assert "order_flow_imbalance" in snap["ml_impact"]
    assert snap["algo_stats"] is not None
