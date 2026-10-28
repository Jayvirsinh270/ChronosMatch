"""
ChronosMatch: Test Suite for Interactive Web GUI Server.
Verifies WebTradingServer order placement, Level 2 depth snapshotting,
and matching execution flow.
"""

import os
from pathlib import Path
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
