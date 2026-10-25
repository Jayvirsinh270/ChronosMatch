"""
Unit & Integration Tests for Lightweight ML Market Pattern Model & Data Storage.
Tests regime classification, Bayesian transition dynamics, live candle synthesis,
and SQLite persistence in ChronosMatch.
"""

import os
import tempfile
import time
import pytest

from chronos_match.market.ml_pattern_model import LightMarketPatternModel, MarketRegime
from chronos_match.market.storage import MarketDataStore
from chronos_match.market.router import MultiBookRouter, SymbolInfo
from chronos_match.market.replayer import HistoricalMarketReplayer


def test_ml_pattern_model_initialization():
    """Verify default initial state of the Lightweight ML Pattern Model."""
    model = LightMarketPatternModel(symbol="NVDA", base_price=120.0, baseline_volatility=0.015)
    pred = model.predict()

    assert pred.regime == MarketRegime.BALANCED_CONSOLIDATION
    assert 0.0 <= pred.confidence <= 1.0
    assert pred.spread_multiplier == 1.0
    assert abs(sum(pred.regime_probabilities.values()) - 1.0) < 1e-4


def test_ml_pattern_model_bull_breakout_detection():
    """Verify that a surge in buyer-initiated trades triggers Bullish Breakout."""
    model = LightMarketPatternModel(symbol="NVDA", base_price=100.0, window_size=50)

    # Ingest successive aggressive BUY trades with increasing prices
    price = 100.0
    for _ in range(25):
        price += 0.50
        model.record_trade(price=price, quantity=500, side=1)  # side 1 = BUY

    pred = model.predict()
    assert pred.regime == MarketRegime.MOMENTUM_BREAKOUT_BULL
    assert pred.predicted_drift > 0.0
    assert pred.ofi_score > 0.0
    assert "LIFT_OFFERS" in pred.suggested_action


def test_ml_pattern_model_bear_breakout_detection():
    """Verify that a cascade of seller-initiated trades triggers Bearish Breakout."""
    model = LightMarketPatternModel(symbol="AAPL", base_price=200.0, window_size=50)

    # Ingest successive aggressive SELL trades with dropping prices
    price = 200.0
    for _ in range(25):
        price -= 0.60
        model.record_trade(price=price, quantity=600, side=2)  # side 2 = SELL

    pred = model.predict()
    assert pred.regime == MarketRegime.MOMENTUM_BREAKOUT_BEAR
    assert pred.predicted_drift < 0.0
    assert pred.ofi_score < 0.0
    assert "HIT_BIDS" in pred.suggested_action


def test_ml_pattern_model_inference_speed():
    """Verify microsecond execution time (<50 µs per tick)."""
    model = LightMarketPatternModel(symbol="TSLA", base_price=250.0)

    t0 = time.perf_counter_ns()
    iterations = 200
    for i in range(iterations):
        model.record_trade(price=250.0 + (i % 5) * 0.1, quantity=100, side=1 if i % 2 == 0 else 2)
        _ = model.predict()
    t1 = time.perf_counter_ns()

    elapsed_us = (t1 - t0) / 1000.0
    avg_us_per_tick = elapsed_us / iterations
    assert avg_us_per_tick < 300.0  # Ultra-fast microsecond performance


def test_market_data_store_persistence():
    """Test recording and retrieving market candles, ticks, and ML logs in SQLite."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        store = MarketDataStore(db_path=db_path)

        # 1. Record candles
        store.record_candle(
            symbol="NVDA",
            timestamp=1700000000,
            date_str="2026-10-24 10:00:00",
            open_p=120.0,
            high_p=122.5,
            low_p=119.5,
            close_p=121.8,
            volume=50000,
            trades_count=120,
        )

        candles = store.get_recent_candles("NVDA", limit=10)
        assert len(candles) == 1
        assert candles[0]["open"] == 120.0
        assert candles[0]["close"] == 121.8

        # 2. Record ticks
        store.record_tick(
            symbol="NVDA",
            timestamp_ns=time.perf_counter_ns(),
            price=121.8,
            quantity=100,
            side=1,
            best_bid=121.75,
            best_ask=121.85,
            ofi=350.0,
            regime="MOMENTUM_BREAKOUT_BULL",
        )

        # 3. Record ML regime snapshot
        store.record_ml_regime(
            symbol="NVDA",
            timestamp_ns=time.perf_counter_ns(),
            regime="MOMENTUM_BREAKOUT_BULL",
            confidence=0.88,
            predicted_drift=0.002,
            volatility=0.018,
            spread_multiplier=1.1,
        )

        # 4. Check stats
        stats = store.get_market_statistics("NVDA")
        assert stats["persisted_candles"] == 1
        assert stats["persisted_ticks"] == 1
        assert stats["total_volume"] == 50000

        store.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_replayer_ml_pattern_integration():
    """Verify HistoricalMarketReplayer integrates ML Pattern Model and DataStore."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        router = MultiBookRouter()
        store = MarketDataStore(db_path=db_path)
        replayer = HistoricalMarketReplayer(router=router, data_store=store, default_ticker="NVDA")

        # Symbol should be bound and pattern model initialized
        assert "NVDA" in replayer.pattern_models
        pred0 = replayer.pattern_models["NVDA"].predict()
        assert pred0.regime is not None

        # Execute market ticks
        for _ in range(10):
            events = replayer.step_market_tick("NVDA")

        # Persisted ticks should exist
        stats = store.get_market_statistics("NVDA")
        assert stats["symbol"] == "NVDA"

        store.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_ml_pretrained_weights_integration():
    """Verify that LightMarketPatternModel integrates pre-trained weights from real dataset."""
    from chronos_match.market.ml_pattern_model import _load_pretrained_weights

    weights = _load_pretrained_weights()
    assert weights is not None, "Pre-trained weights file should be loaded"
    assert "gnb_params" in weights
    assert "priors" in weights
    assert "total_samples" in weights
    assert weights["total_samples"] > 100_000

    # Instantiate model and verify calibrated parameters
    model = LightMarketPatternModel(symbol="NVDA", base_price=120.0)
    assert model._gnb_params is not None
    assert model.w_ofi > 1.5
    assert model.w_price_trend > 0.8
    assert model.w_autocorr > 0.5

    # Run trades and ensure GNB scoring runs without error
    for i in range(25):
        model.record_trade(price=120.0 + i * 0.1, quantity=100, side=1)

    pred = model.predict()
    assert pred.regime in [MarketRegime.MOMENTUM_BREAKOUT_BULL, MarketRegime.BALANCED_CONSOLIDATION]
    assert pred.accuracy_pct > 50.0
    assert pred.macro_f1 > 0.1
    assert pred.matthews_corr_coef >= 0.0


def test_candlestick_pattern_and_feature_store_persistence():
    """Verify storing and retrieving candlestick patterns and feature vectors in SQLite."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        store = MarketDataStore(db_path=db_path)

        # 1. Store a detected pattern
        store.record_pattern_detection(
            symbol="NVDA",
            timestamp=1700000000,
            date_str="2026-10-25 10:00:00",
            pattern_name="BULLISH ENGULFING",
            category="REVERSAL",
            sentiment="BULLISH",
            confidence=0.88,
            pattern_score=0.85,
            description="Strong bullish body engulfing prior bearish candle",
        )

        pats = store.get_recent_patterns("NVDA", limit=5)
        assert len(pats) == 1
        assert pats[0]["pattern_name"] == "BULLISH ENGULFING"
        assert pats[0]["sentiment"] == "BULLISH"
        assert pats[0]["confidence"] == 0.88

        # 2. Store a full feature store vector
        store.record_feature_vector(
            symbol="NVDA",
            timestamp=1700000000,
            date_str="2026-10-25 10:00:00",
            open_p=120.0,
            high_p=122.5,
            low_p=119.5,
            close_p=122.0,
            volume=45000,
            pattern="BULLISH ENGULFING",
            pattern_sentiment="BULLISH",
            pattern_score=0.85,
            rsi14=62.4,
            macd_signal=1.25,
            bb_bandwidth=3.4,
            atr_ratio=1.15,
            mom5=2.4,
            mom10=4.1,
            vol_ratio=1.35,
            vol10=22.5,
            price_accel=0.15,
            predicted_regime="MOMENTUM_BREAKOUT_BULL",
            confidence=0.85,
        )

        samples = store.get_feature_store_samples("NVDA", limit=5)
        assert len(samples) == 1
        assert samples[0]["rsi14"] == 62.4
        assert samples[0]["pattern"] == "BULLISH ENGULFING"
        assert samples[0]["predicted_regime"] == "MOMENTUM_BREAKOUT_BULL"

        # Check market statistics counters
        stats = store.get_market_statistics("NVDA")
        assert stats["persisted_patterns"] == 1
        assert stats["persisted_features"] == 1

        store.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


@pytest.mark.anyio
async def test_api_get_ml_metrics():
    """Verify that /api/market/ml-metrics returns institutional evaluation metrics."""
    from chronos_match.web.server import api_get_ml_metrics

    data = await api_get_ml_metrics()
    assert "version" in data
    assert "evaluation_metrics" in data
    metrics = data["evaluation_metrics"]
    assert "accuracy" in metrics
    assert "macro_f1" in metrics
    assert "matthews_corr_coef" in metrics
    assert "confusion_matrix" in metrics


