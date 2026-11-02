"""
Unit & Integration Tests for:
1. Repetitive user trades (preventing flatline / stuck states)
2. Asymmetric whale order impact & overcoming market selling flow
3. 1-Day Buy/Sell trade tape CSV download endpoint (/api/market/export-csv)
4. Quantitative Microstructure Feature Store export endpoint (/api/market/export-features-csv)
"""

import asyncio
import csv
import io
import time
import pytest

from chronos_match.market.router import MultiBookRouter
from chronos_match.market.replayer import HistoricalMarketReplayer
from chronos_match.protocol import OrderSide, to_fixed_price, from_fixed_price
from chronos_match.web.server import api_export_market_csv, api_export_features_csv


def test_user_repetitive_trades_never_stuck():
    """Verify repetitive user trades continually move price without flatlining into a single line."""
    router = MultiBookRouter()
    replayer = HistoricalMarketReplayer(router=router, default_ticker="SOL-USD")
    venue = replayer.bind_stock("SOL-USD")

    initial_p = replayer.current_mid.get("SOL-USD", 150.0)

    # 1. Repetitive BUY trades (10 in a row)
    prices_buy = []
    for _ in range(10):
        new_p = replayer.apply_user_trade_impact(
            symbol="SOL-USD",
            side=int(OrderSide.BUY),
            quantity=100,
            execution_price=replayer.current_mid["SOL-USD"],
        )
        prices_buy.append(new_p)

    # Price should consistently advance upwards without getting stuck
    assert prices_buy[-1] > initial_p
    assert prices_buy[-1] > prices_buy[0]
    # Check that prices are not all identical (no flatline)
    assert len(set(prices_buy)) >= 4, f"Prices got stuck in flatline: {prices_buy}"

    # Verify order book depth remains intact and positive
    assert from_fixed_price(venue.book.best_bid_price) > 0
    assert from_fixed_price(venue.book.best_ask_price) > 0
    assert from_fixed_price(venue.book.best_ask_price) >= from_fixed_price(venue.book.best_bid_price)

    # 2. Repetitive SELL trades (10 in a row)
    peak_p = replayer.current_mid["SOL-USD"]
    prices_sell = []
    for _ in range(10):
        new_p = replayer.apply_user_trade_impact(
            symbol="SOL-USD",
            side=int(OrderSide.SELL),
            quantity=100,
            execution_price=replayer.current_mid["SOL-USD"],
        )
        prices_sell.append(new_p)

    assert prices_sell[-1] < peak_p
    assert len(set(prices_sell)) >= 4, f"Sell prices got stuck in flatline: {prices_sell}"


def test_whale_dump_and_large_buy_overcoming_sells():
    """Verify that whale orders crash the market and large buys turn selling candles green."""
    router = MultiBookRouter()
    replayer = HistoricalMarketReplayer(router=router, default_ticker="BTC-USD")
    venue = replayer.bind_stock("BTC-USD")

    base_p = 65000.0
    replayer.current_mid["BTC-USD"] = base_p

    # 1. Large user dump: 5,000 shares
    dump_p = replayer.apply_user_trade_impact(
        symbol="BTC-USD",
        side=int(OrderSide.SELL),
        quantity=5000,
        execution_price=base_p,
    )

    # Price should drop significantly
    assert dump_p < base_p * 0.99, f"Expected substantial price drop, got {dump_p} from {base_p}"
    w_state = replayer.wave_states.get("BTC-USD")
    assert w_state.phase == "BEAR_IMPULSE"

    # Candle should be RED
    bar = replayer.live_candle.get("BTC-USD")
    assert bar is not None
    assert bar["close"] < bar["open"]

    # 2. Large user buy overcoming market selling: 8,000 shares
    rebound_p = replayer.apply_user_trade_impact(
        symbol="BTC-USD",
        side=int(OrderSide.BUY),
        quantity=8000,
        execution_price=dump_p,
    )

    assert rebound_p > dump_p
    # Live candle must turn GREEN
    bar_after = replayer.live_candle.get("BTC-USD")
    assert bar_after["close"] > bar_after["open"], f"Candle should be green: close={bar_after['close']} open={bar_after['open']}"


def test_api_export_market_csv():
    """Verify endpoint /api/market/export-csv returns valid 1-day buy & sell CSV."""
    resp = asyncio.run(api_export_market_csv(symbol="SOL-USD"))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in resp.headers.get("content-disposition", "")

    # Parse CSV content
    reader = csv.reader(io.StringIO(resp.body.decode("utf-8")))
    rows = list(reader)
    assert len(rows) >= 2, "Expected header and at least one trade row"
    header = rows[0]
    assert "trade_id" in header
    assert "symbol" in header
    assert "side" in header
    assert "price" in header
    assert "quantity" in header


def test_api_export_features_csv():
    """Verify endpoint /api/market/export-features-csv returns quantitative feature store rows."""
    resp = asyncio.run(api_export_features_csv(symbol="SOL-USD"))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")

    reader = csv.reader(io.StringIO(resp.body.decode("utf-8")))
    rows = list(reader)
    assert len(rows) >= 1
    header = rows[0]
    assert "rsi14" in header
    assert "predicted_regime" in header
    assert "confidence" in header
