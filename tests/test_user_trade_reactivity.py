"""
Tests for User Trade Reactivity, Market Impact, and Algorithmic Adaptation.

Verifies:
1. User market buy/sell orders exert authentic price impact and continuity.
2. Market maker algorithm (Avellaneda-Stoikov) reacts by shifting inventory and reservation prices.
3. Active candlestick reflects user trade, and subsequent candle opens at that price level.
4. User resting limit orders are filled by market sweeps and correctly settle in Risk Engine.
"""

import pytest
from chronos_match.market.router import MultiBookRouter, SymbolInfo
from chronos_match.market.replayer import HistoricalMarketReplayer
from chronos_match.risk.risk_engine import PreTradeRiskEngine
from chronos_match.web.server import WebTradingServer
from chronos_match.protocol import OrderSide, to_fixed_price, from_fixed_price


def test_user_trade_market_impact_and_candle_update():
    """Verify user buy shifts current mid upwards and updates live candlestick."""
    router = MultiBookRouter()
    risk = PreTradeRiskEngine()
    replayer = HistoricalMarketReplayer(router=router, default_ticker="SOL-USD", risk_engine=risk)

    sym = "SOL-USD"
    venue = router.get_venue(sym)
    initial_mid = replayer.current_mid[sym]
    assert initial_mid > 0

    # User buys 500 shares at initial_mid + 1.00
    exec_price = round(initial_mid + 1.00, 2)
    new_mid = replayer.apply_user_trade_impact(
        symbol=sym,
        side=int(OrderSide.BUY),
        quantity=500,
        execution_price=exec_price,
    )

    # 1. Price moved up
    assert new_mid > initial_mid
    assert replayer.current_mid[sym] == new_mid

    # 2. Live candle reflects user trade
    candle = replayer.live_candle[sym]
    assert candle["close"] == new_mid
    assert candle["high"] >= new_mid
    assert candle["volume"] >= 500

    # 3. Wave state triggered bull impulse on large buy
    w_state = replayer.wave_states[sym]
    assert w_state.phase == "BULL_IMPULSE"


def test_algo_reacts_to_user_trade():
    """Verify market maker skews quotes when user trades."""
    router = MultiBookRouter()
    risk = PreTradeRiskEngine()
    replayer = HistoricalMarketReplayer(router=router, default_ticker="SOL-USD", risk_engine=risk)

    sym = "SOL-USD"
    algo_mgr = replayer.algo_managers[sym]
    algo_mgr.toggle_bot("mm", enable=True)

    mid_start = replayer.current_mid[sym]
    res_start, bid_start, ask_start = algo_mgr.market_maker.compute_quotes(mid_start, 0.02)

    # User buys 1,000 shares
    replayer.apply_user_trade_impact(
        symbol=sym,
        side=int(OrderSide.BUY),
        quantity=1000,
        execution_price=mid_start + 0.50,
    )

    # MM inventory should have decreased by 1000 (MM sold to user)
    assert algo_mgr.market_maker.inventory == -1000

    # Next compute_quotes should reflect higher reservation price and higher quotes
    res_after, bid_after, ask_after = algo_mgr.market_maker.compute_quotes(
        replayer.current_mid[sym],
        0.02,
        replayer.impact_engines[sym],
    )
    assert res_after > res_start
    assert ask_after > ask_start


def test_user_resting_order_settled_by_market_sweep():
    """Verify user resting limit orders are matched in replayer and settled in RiskEngine."""
    server = WebTradingServer(enable_wal=False)
    venue = server.router.get_venue("SOL-USD")

    # Fund user (client 1)
    acc = server.risk_engine.get_or_create_account(1, initial_cash=100_000.0, initial_shares=1000, symbol_id=venue.info.symbol_id)
    initial_cash = acc.cash_balance
    initial_shares = acc.positions.get(venue.info.symbol_id, 0)

    # Place resting limit buy at price higher than best bid but lower than best ask (top of book!)
    best_bid = from_fixed_price(venue.book.best_bid_price)
    best_ask = from_fixed_price(venue.book.best_ask_price)
    buy_p = round((best_bid + best_ask) / 2.0, 2)
    if buy_p <= best_bid:
        buy_p = round(best_bid + venue.info.tick_size, 2)
    if buy_p >= best_ask:
        buy_p = best_bid

    order_res = server.place_order(
        side_str="BUY",
        price_float=buy_p,
        quantity=100,
        client_id=1,
        type_str="LIMIT",
        tif_str="GTC",
        symbol="SOL-USD",
    )
    user_oid = order_res["order_id"]
    assert user_oid in venue.user_open_orders
    assert venue.user_open_orders[user_oid]["remaining"] == 100

    # Simulate market sweep that sells into user's bid (now at top of book)
    matches = venue.book.add_limit_order(
        order_id=server.router.allocate_order_id(),
        client_id=99,
        price=to_fixed_price(buy_p),
        quantity=100,
        side=int(OrderSide.SELL),
        symbol_id=venue.info.symbol_id,
    )
    assert len(matches) > 0
    assert any(m["buy_order_id"] == user_oid for m in matches)

    # Deliver match to replayer
    for m in matches:
        trade_p = from_fixed_price(m["execution_price"])
        server.replayer._on_trade_matched(
            venue, "SOL-USD", int(OrderSide.SELL), trade_p, m["quantity"],
            server.replayer.impact_engines["SOL-USD"],
            server.replayer.pattern_models["SOL-USD"],
            server.replayer.algo_managers["SOL-USD"],
            m,
        )

    # Order should be completely filled and removed from user_open_orders
    assert user_oid not in venue.user_open_orders

    # Cash and positions should be updated in Risk Engine
    assert acc.cash_balance < initial_cash
    assert acc.positions[venue.info.symbol_id] == initial_shares + 100
