"""
ChronosMatch: Test Suite for Whale Sweep & SLA Breach Detector.
Verifies institutional block detection, multi-level sweep aggregation,
and nanosecond SLA latency breach alerting.
"""

import time
import pytest

from chronos_match.protocol import TradePacket, to_fixed_price, Side
from chronos_match.monitor.whale_detector import WhaleDetector, WhaleAlert


def test_whale_detector_normal_trade():
    """Verify normal retail orders do not trigger alerts."""
    detector = WhaleDetector(
        volume_threshold=5000,
        notional_threshold=250_000.0,
        sla_latency_threshold_ns=50_000
    )

    trade = TradePacket(
        trade_id=1,
        buy_order_id=10,
        sell_order_id=20,
        price=to_fixed_price(150.00),
        quantity=200,  # $30,000 notional
        symbol_id=1,
        aggressor_side=int(Side.BUY),
        match_timestamp_ns=time.perf_counter_ns(),
        latency_ns=4_500,  # 4.5 μs
    )

    alert = detector.inspect_trade(trade)
    assert alert is None
    assert detector.total_alerts == 0


def test_whale_detector_large_block():
    """Verify single large block institutional executions trigger LARGE_BLOCK alert."""
    detector = WhaleDetector(
        volume_threshold=5000,
        notional_threshold=250_000.0,
        sla_latency_threshold_ns=50_000
    )

    # 1. Trigger via volume >= 5,000
    t_vol = TradePacket(
        trade_id=2,
        buy_order_id=100,
        sell_order_id=200,
        price=to_fixed_price(50.00),
        quantity=6_000,
        symbol_id=1,
        aggressor_side=int(Side.SELL),
        match_timestamp_ns=time.perf_counter_ns(),
        latency_ns=8_000,
    )
    a1 = detector.inspect_trade(t_vol)
    assert a1 is not None
    assert a1.alert_type == "LARGE_BLOCK"
    assert a1.quantity == 6_000
    assert a1.aggressor_side == "SELL"
    assert a1.price == 50.00
    assert a1.notional_usd == 300_000.0

    # 2. Trigger via notional >= $250,000 even with < 5,000 shares
    t_notional = TradePacket(
        trade_id=3,
        buy_order_id=101,
        sell_order_id=201,
        price=to_fixed_price(500.00),
        quantity=1_000,  # $500,000 notional
        symbol_id=1,
        aggressor_side=int(Side.BUY),
        match_timestamp_ns=time.perf_counter_ns(),
        latency_ns=6_200,
    )
    a2 = detector.inspect_trade(t_notional)
    assert a2 is not None
    assert a2.alert_type == "LARGE_BLOCK"
    assert a2.notional_usd == 500_000.0


def test_whale_detector_multi_level_sweep():
    """Verify multiple fills from a single aggressor order within microsecond window trigger WHALE_SWEEP."""
    detector = WhaleDetector(
        volume_threshold=5000,
        notional_threshold=250_000.0,
        sla_latency_threshold_ns=50_000
    )

    aggressor_buy_id = 9999
    base_time = time.perf_counter_ns()

    # Fill 1: 500 shares
    t1 = TradePacket(
        trade_id=10,
        buy_order_id=aggressor_buy_id,
        sell_order_id=101,
        price=to_fixed_price(150.00),
        quantity=500,
        symbol_id=1,
        aggressor_side=int(Side.BUY),
        match_timestamp_ns=base_time,
        latency_ns=5_000,
    )
    assert detector.inspect_trade(t1) is None

    # Fill 2: 500 shares (same aggressor, 20 microseconds later)
    t2 = TradePacket(
        trade_id=11,
        buy_order_id=aggressor_buy_id,
        sell_order_id=102,
        price=to_fixed_price(150.10),
        quantity=500,
        symbol_id=1,
        aggressor_side=int(Side.BUY),
        match_timestamp_ns=base_time + 20_000,
        latency_ns=5_100,
    )
    assert detector.inspect_trade(t2) is None

    # Fill 3: 500 shares (same aggressor, 40 microseconds later) -> triggers sweep!
    t3 = TradePacket(
        trade_id=12,
        buy_order_id=aggressor_buy_id,
        sell_order_id=103,
        price=to_fixed_price(150.20),
        quantity=500,
        symbol_id=1,
        aggressor_side=int(Side.BUY),
        match_timestamp_ns=base_time + 40_000,
        latency_ns=5_200,
    )
    a3 = detector.inspect_trade(t3)
    assert a3 is not None
    assert a3.alert_type == "WHALE_SWEEP"
    assert a3.quantity == 1500  # Total accumulated across the 3 fills
    assert "Multi-Level Sweep: 3 consecutive fills" in a3.details


def test_whale_detector_sla_breach():
    """Verify execution latency exceeding SLA threshold triggers SLA_BREACH alert."""
    detector = WhaleDetector(
        volume_threshold=5000,
        notional_threshold=250_000.0,
        sla_latency_threshold_ns=50_000  # 50 μs SLA
    )

    trade = TradePacket(
        trade_id=20,
        buy_order_id=301,
        sell_order_id=401,
        price=to_fixed_price(100.00),
        quantity=100,
        symbol_id=1,
        aggressor_side=int(Side.SELL),
        match_timestamp_ns=time.perf_counter_ns(),
        latency_ns=72_500,  # 72.5 μs > 50 μs
    )

    alert = detector.inspect_trade(trade)
    assert alert is not None
    assert alert.alert_type == "SLA_BREACH"
    assert alert.latency_us == 72.5
    assert "SLA Breach" in alert.details


def test_whale_detector_history_and_statistics():
    """Verify statistics counters and circular history buffer."""
    detector = WhaleDetector(max_history=5)

    for i in range(1, 10):
        # alternate between block and SLA breach
        lat = 60_000 if i % 2 == 0 else 5_000
        qty = 8_000 if i % 2 != 0 else 100
        t = TradePacket(
            trade_id=i,
            buy_order_id=i * 10,
            sell_order_id=i * 20,
            price=to_fixed_price(100.0),
            quantity=qty,
            symbol_id=1,
            aggressor_side=int(Side.BUY),
            match_timestamp_ns=time.perf_counter_ns(),
            latency_ns=lat,
        )
        detector.inspect_trade(t)

    assert detector.total_alerts == 9
    assert len(detector.get_recent_alerts(10)) == 5  # capped by max_history=5
    stats = detector.get_statistics()
    assert stats["total_alerts"] == 9
    assert stats["active_alerts_in_window"] == 5
