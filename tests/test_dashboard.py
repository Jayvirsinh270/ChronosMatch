"""
ChronosMatch: Test Suite for Real-Time Terminal Dashboard (Rich TUI).
Verifies layout structure, Level 2 depth ladder rendering, KPI cards,
trade execution ticker formatting, and whale alert panels.
"""

import time
import pytest

from chronos_match.protocol import to_fixed_price, Side
from chronos_match.engine.order_book_cy import FastOrderBook
from chronos_match.monitor.metrics import LatencyProfiler, LatencyStats
from chronos_match.monitor.whale_detector import WhaleDetector, WhaleAlert
from chronos_match.monitor.dashboard import TerminalDashboard


def test_dashboard_build_layout_structure():
    """Verify Rich Layout structure and nested children."""
    dashboard = TerminalDashboard()
    layout = dashboard.build_layout()

    assert layout["header"] is not None
    assert layout["kpis"] is not None
    assert layout["main_body"] is not None
    assert layout["alerts"] is not None
    assert layout["order_book"] is not None
    assert layout["trade_ticker"] is not None


def test_dashboard_render_string_empty_state():
    """Verify dashboard renders cleanly without exceptions on an empty order book."""
    dashboard = TerminalDashboard()
    output = dashboard.render_string(
        l2_snapshot={"bids": [], "asks": []},
        latency_stats=None,
        throughput_info=None,
        recent_trades=[],
        whale_alerts=[],
    )

    assert "CHRONOS MATCH" in output
    assert "LEVEL 2" in output
    assert "LIVE TRADE EXECUTION" in output
    assert "Book Empty" in output or "Empty Book" in output
    assert "NOMINAL" in output


def test_dashboard_render_string_with_active_data():
    """Verify dashboard rendering with active L2 depth, trades, and latency metrics."""
    dashboard = TerminalDashboard()

    l2_snapshot = {
        "bids": [
            {"price": to_fixed_price(150.25), "volume": 1200, "order_count": 4},
            {"price": to_fixed_price(150.20), "volume": 3500, "order_count": 8},
        ],
        "asks": [
            {"price": to_fixed_price(150.30), "volume": 1500, "order_count": 5},
            {"price": to_fixed_price(150.35), "volume": 4200, "order_count": 12},
        ],
    }

    latency_stats = LatencyStats(
        sample_count=10_000,
        min_us=1.2,
        max_us=48.5,
        mean_us=4.3,
        p50_us=3.8,
        p90_us=8.1,
        p99_us=18.2,
        p999_us=32.0,
    )

    throughput_info = {
        "orders_processed": 50_000,
        "cancellations_processed": 12_500,
        "trades_generated": 8_400,
        "shares_traded": 1_250_000,
        "orders_per_sec": 125_000.0,
        "best_bid": to_fixed_price(150.25),
        "best_ask": to_fixed_price(150.30),
        "spread": to_fixed_price(0.05),
    }

    recent_trades = [
        {
            "trade_id": 101,
            "aggressor_side": int(Side.BUY),
            "price": to_fixed_price(150.30),
            "quantity": 500,
            "latency_ns": 4_200,
        },
        {
            "trade_id": 102,
            "aggressor_side": int(Side.SELL),
            "price": to_fixed_price(150.25),
            "quantity": 300,
            "latency_ns": 3_900,
        },
    ]

    whale_alerts = [
        WhaleAlert(
            alert_id=1,
            timestamp_ns=time.perf_counter_ns(),
            trade_id=101,
            buy_order_id=5001,
            sell_order_id=6001,
            symbol_id=1,
            aggressor_side="BUY",
            price=150.30,
            quantity=10_000,
            notional_usd=1_503_000.0,
            latency_us=4.2,
            alert_type="LARGE_BLOCK",
            details="Block Execution: 10,000 shares @ $150.30",
        )
    ]

    output = dashboard.render_string(
        l2_snapshot=l2_snapshot,
        latency_stats=latency_stats,
        throughput_info=throughput_info,
        recent_trades=recent_trades,
        whale_alerts=whale_alerts,
        width=140,
    )

    assert "CHRONOS MATCH" in output
    assert "150.25" in output
    assert "150.30" in output
    assert "SPREAD: $0.05" in output or "$0.05" in output
    assert "p50: 3.80μs" in output
    assert "p99: 18.20μs" in output
    assert "SLA TARGET MET" in output
    assert "LARGE_BLOCK" in output
    assert "1,503,000" in output


def test_dashboard_integration_with_fast_order_book():
    """Verify live snapshot extraction from FastOrderBook into the Dashboard."""
    book = FastOrderBook(arena_capacity=10_000)

    # Place resting bids and asks
    book.add_limit_order(1, 10, to_fixed_price(100.00), 500, int(Side.BUY))
    book.add_limit_order(2, 11, to_fixed_price(99.50), 1000, int(Side.BUY))
    book.add_limit_order(3, 12, to_fixed_price(100.50), 750, int(Side.SELL))
    book.add_limit_order(4, 13, to_fixed_price(101.00), 1250, int(Side.SELL))

    snapshot = book.get_l2_snapshot(depth=5)
    assert len(snapshot["bids"]) == 2
    assert len(snapshot["asks"]) == 2

    dashboard = TerminalDashboard()
    rendered = dashboard.render_string(
        l2_snapshot=snapshot,
        throughput_info={
            "best_bid": book.best_bid_price,
            "best_ask": book.best_ask_price,
            "spread": book.spread,
        },
    )

    assert "100.00" in rendered
    assert "100.50" in rendered
    assert "SPREAD: $0.50" in rendered or "$0.50" in rendered
