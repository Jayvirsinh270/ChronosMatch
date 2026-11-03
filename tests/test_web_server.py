"""
ChronosMatch: Test Suite for Interactive Web GUI Server.
Verifies WebTradingServer order placement, Level 2 depth snapshotting,
and matching execution flow.
"""

import os
from pathlib import Path
import time
import pytest

from chronos_match.web.server import WebTradingServer


def test_web_html_template_exists():
    """Verify that index.html exists and contains core UI components."""
    html_file = Path(__file__).parent.parent / "chronos_match" / "web" / "index.html"
    assert html_file.exists()
    content = html_file.read_text(encoding="utf-8")
    assert "CHRONOS MATCH" in content
    assert "ORDER ENTRY FORM" in content
    assert "LEVEL 2 MARKET DEPTH LADDER" in content
    assert "LIVE TRADES & WHALES" in content


def test_web_server_initial_snapshot():
    """Verify WebTradingServer initializes with seeded two-sided liquidity."""
    server = WebTradingServer(db_path=":memory:")
    snap = server.get_snapshot()

    assert snap is not None
    assert "l2" in snap
    assert len(snap["l2"]["bids"]) > 0
    assert len(snap["l2"]["asks"]) > 0
    assert snap["best_bid"] > 0
    assert snap["best_ask"] > 0
    assert snap["spread"] > 0
    assert snap["total_orders"] > 0


def test_web_server_place_resting_limit_order():
    """Verify placing a resting limit order updates the Level 2 book without fills."""
    server = WebTradingServer(db_path=":memory:")

    # Place a deep passive Buy order at $140.00 (below current bids)
    res = server.place_order(
        side_str="BUY",
        type_str="LIMIT",
        price_float=140.00,
        quantity=300,
    )

    assert res["success"] is True
    assert res["side"] == "BUY"
    assert res["price"] == 140.00
    assert res["quantity"] == 300
    assert res["total_filled"] == 0
    assert len(res["matched_trades"]) == 0
    assert res["latency_us"] > 0


def test_web_server_market_order_crossing_spread():
    """Verify an aggressive market order sweeps passive asks and emits match records."""
    server = WebTradingServer(db_path=":memory:")

    # Place aggressive Market Buy order
    res = server.place_order(
        side_str="BUY",
        type_str="MARKET",
        price_float=150.00,
        quantity=500,
    )

    assert res["success"] is True
    assert res["total_filled"] == 500
    assert len(res["matched_trades"]) > 0
    assert res["latency_us"] > 0

    snap = server.get_snapshot()
    assert snap["total_trades"] > 0
    assert len(snap["recent_trades"]) > 0
    assert snap["recent_trades"][0]["quantity"] > 0


def test_web_server_l2_depth_ladder_persistence():
    """Verify that asks and bids in Level 2 depth ladder remain populated over continuous trading ticks."""
    server = WebTradingServer(db_path=":memory:")
    sym = server.active_symbol

    for tick in range(1, 301):
        server.replayer.step_market_tick(sym)
        snap = server.get_snapshot()
        l2 = snap["l2"]
        bids = l2["bids"]
        asks = l2["asks"]
        assert len(asks) > 0, f"Asks vanished at tick {tick}"
        assert len(bids) > 0, f"Bids vanished at tick {tick}"
        assert snap["best_ask"] >= snap["best_bid"], f"Crossed book at tick {tick}"


def test_export_csv_durations_and_stocks():
    """Verify CSV export endpoint supports 30m, 1h, 2h duration filters and ALL stocks."""
    from chronos_match.web.server import api_export_market_csv

    # 1. Export 2h trades for ALL stocks
    resp1 = api_export_market_csv(symbol="ALL", duration="2h", data_type="trades")
    lines1 = resp1.body.decode("utf-8").strip().split("\n")
    assert len(lines1) >= 1
    assert "trade_id" in lines1[0]
    assert "symbol" in lines1[0]
    assert "best_bid" in lines1[0]

    # 2. Export 1h trades for active symbol
    resp2 = api_export_market_csv(symbol="NVDA-USD", duration="1h", data_type="trades")
    lines2 = resp2.body.decode("utf-8").strip().split("\n")
    assert len(lines2) >= 1
    assert "trade_id" in lines2[0]

    # 3. Export 30m candles
    resp3 = api_export_market_csv(symbol="ALL", duration="30m", data_type="candles")
    lines3 = resp3.body.decode("utf-8").strip().split("\n")
    assert len(lines3) >= 1
    assert "candle_id" in lines3[0]
    assert "open" in lines3[0]
    assert "close" in lines3[0]


def test_candlestick_persistence_resumes_from_db(tmp_path):
    """Verify that when candles are stored in DB, re-opening server resumes from exact last close."""
    from chronos_match.market.storage import MarketDataStore
    from chronos_match.market.router import MultiBookRouter, SymbolInfo
    from chronos_match.market.replayer import HistoricalMarketReplayer

    db_path = str(tmp_path / "persist_test.db")
    store = MarketDataStore(db_path=db_path)

    # Store 15 candles for BTC-USD ending at $61,395.50
    for i in range(15):
        store.record_candle(
            symbol="BTC-USD",
            timestamp=1700000000 + i * 60,
            date_str=f"10:{i:02d}:00",
            open_p=62000.0 - i * 40,
            high_p=62050.0 - i * 40,
            low_p=61950.0 - i * 40,
            close_p=62000.0 - i * 40 if i < 14 else 61395.50,
            volume=800,
            trades_count=30,
        )

    router = MultiBookRouter()
    replayer = HistoricalMarketReplayer(router=router, data_store=store, default_ticker="BTC-USD")
    candles = replayer.get_candlesticks("BTC-USD", limit=50)

    # Verify loaded from DB and resumed at 61395.50
    assert len(candles) >= 15
    last_candle = candles[-1]
    assert abs(last_candle["close"] - 61395.50) < 0.01
    assert abs(replayer.current_mid["BTC-USD"] - 61395.50) < 0.01

    store.close()


def test_all_stocks_navigation_and_order_routing():
    """Verify that all traded company stocks are in the UI navigation and support order routing."""
    html_file = Path(__file__).parent.parent / "chronos_match" / "web" / "index.html"
    assert html_file.exists()
    html_text = html_file.read_text(encoding="utf-8")

    # 1. Verify index.html contains all 8 company tickers in symbolNav
    expected_stocks = ["BTC-USD", "ETH-USD", "SOL-USD", "NVDA", "AAPL", "MSFT", "TSLA", "RELIANCE"]
    for sym in expected_stocks:
        assert f'data-sym="{sym}"' in html_text, f"Missing {sym} pill in web HTML"

    # 2. Verify server registers and routes orders for MSFT and RELIANCE
    server = WebTradingServer(db_path=":memory:")
    for sym in ["MSFT", "RELIANCE", "AAPL", "TSLA", "NVDA-USD", "BTC-USD"]:
        venue = server.router.get_venue(sym)
        assert venue is not None
        assert sym in venue.info.ticker or venue.info.ticker in sym

        # Fetch snapshot for this symbol
        snap = server.get_snapshot(symbol=sym)
        assert snap is not None
        assert "l2" in snap
        assert len(snap["l2"]["bids"]) > 0 or len(snap["l2"]["asks"]) > 0

        # Place a resting limit buy order for MSFT / RELIANCE
        mid_p = snap["last_price"] or venue.info.initial_price
        res_ord = server.place_order(
            side_str="BUY",
            type_str="LIMIT",
            price_float=round(mid_p * 0.99, 2),
            quantity=10,
            symbol=sym,
            client_id=1,
        )
        assert res_ord["success"] is True, f"Failed placing order on {sym}: {res_ord}"


def test_continuous_candlestick_rollover():
    """Verify that candles roll over continuously as time advances without getting stuck."""
    server = WebTradingServer(db_path=":memory:")
    sym = "BTC-USD"
    server.replayer.candle_interval_sec = 0.2
    
    # Run simulation ticks across time steps
    for step in range(12):
        server.replayer.step_market_tick(sym)
        time.sleep(0.08)
        
    candles = server.replayer.get_candlesticks(sym, limit=20)
    assert len(candles) >= 5
    # Verify volumes are natural and not 0
    assert all(c["volume"] > 0 for c in candles)
    # Verify prices are positive and finite
    assert all(c["close"] > 0 for c in candles)


