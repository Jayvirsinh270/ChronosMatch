"""
ChronosMatch: Test Suite for Phase 2 — Pre-Trade Risk Engine & Safety Gatekeeper.

Tests cover:
  - Fat-Finger Price Collar Guard (NBBO midpoint deviation)
  - Max Notional and Quantity Limits
  - Token-Bucket Client Rate Limiter & Flood Protection
  - Account Margin, Purchasing Power, and Position Inventory Ledger
  - Emergency Market Circuit Breaker
  - WebTradingServer Risk Engine Integration
"""

import time
import pytest

from chronos_match.risk.risk_engine import (
    RiskLimits,
    RiskDecision,
    TokenBucketRateLimiter,
    AccountBalance,
    PreTradeRiskEngine,
)
from chronos_match.web.server import WebTradingServer


# ─────────────────────────────────────────────
# 1. FAT-FINGER PRICE COLLAR TESTS
# ─────────────────────────────────────────────

class TestFatFingerPriceCollar:

    def test_price_within_collar_allowed(self):
        """Order priced within ±10% of NBBO mid passes collar check."""
        engine = PreTradeRiskEngine(RiskLimits(price_collar_pct=0.10))
        # Mid is 150.00; 152.00 is 1.33% away (< 10%)
        decision = engine.validate_order(
            order_id=1,
            client_id=10,
            side=0,  # BUY
            price_float=152.00,
            quantity=100,
            best_bid_float=149.95,
            best_ask_float=150.05,
        )
        assert decision.passed is True
        assert decision.reject_reason is None

    def test_price_exceeding_collar_high_rejected(self):
        """Limit order priced > 10% above NBBO mid is rejected as a fat-finger breach."""
        engine = PreTradeRiskEngine(RiskLimits(price_collar_pct=0.10))
        # Mid is 150.00; 170.00 is 13.3% away (> 10%)
        decision = engine.validate_order(
            order_id=2,
            client_id=10,
            side=0,  # BUY
            price_float=170.00,
            quantity=100,
            best_bid_float=149.95,
            best_ask_float=150.05,
        )
        assert decision.passed is False
        assert decision.rule_name == "PRICE_COLLAR"
        assert "PRICE_COLLAR_BREACH" in decision.reject_reason
        assert "170.00" in decision.reject_reason
        assert engine.fat_finger_blocks == 1

    def test_price_exceeding_collar_low_rejected(self):
        """Limit order priced > 10% below NBBO mid is rejected as a fat-finger breach."""
        engine = PreTradeRiskEngine(RiskLimits(price_collar_pct=0.10))
        # Mid is 150.00; 130.00 is 13.3% away (> 10%)
        decision = engine.validate_order(
            order_id=3,
            client_id=10,
            side=1,  # SELL
            price_float=130.00,
            quantity=100,
            best_bid_float=149.95,
            best_ask_float=150.05,
        )
        assert decision.passed is False
        assert decision.rule_name == "PRICE_COLLAR"
        assert "PRICE_COLLAR_BREACH" in decision.reject_reason
        assert "130.00" in decision.reject_reason

    def test_market_order_bypasses_collar(self):
        """Market sweeps are designed to cross the spread and bypass limit price collars."""
        engine = PreTradeRiskEngine(RiskLimits(price_collar_pct=0.10))
        decision = engine.validate_order(
            order_id=4,
            client_id=10,
            side=0,  # BUY
            price_float=200.00,  # Synthetic crossing price
            quantity=100,
            best_bid_float=149.95,
            best_ask_float=150.05,
            is_market=True,
        )
        assert decision.passed is True

    def test_empty_book_bypasses_collar(self):
        """When book has no resting liquidity, collar cannot establish mid and passes."""
        engine = PreTradeRiskEngine(RiskLimits(price_collar_pct=0.10))
        decision = engine.validate_order(
            order_id=5,
            client_id=10,
            side=0,
            price_float=150.00,
            quantity=100,
            best_bid_float=0.0,
            best_ask_float=0.0,
        )
        assert decision.passed is True


# ─────────────────────────────────────────────
# 2. MAX NOTIONAL & QUANTITY LIMIT TESTS
# ─────────────────────────────────────────────

class TestOrderSizeLimits:

    def test_order_within_limits_passed(self):
        """Order within size limits passes successfully."""
        engine = PreTradeRiskEngine(RiskLimits(max_notional_per_order=1_000_000.0, max_quantity_per_order=50_000))
        decision = engine.validate_order(
            order_id=10,
            client_id=1,
            side=0,
            price_float=150.00,
            quantity=1_000,  # Notional: $150,000 < $1,000,000
        )
        assert decision.passed is True

    def test_order_exceeding_max_quantity_rejected(self):
        """Order with quantity exceeding max limit is rejected."""
        engine = PreTradeRiskEngine(RiskLimits(max_quantity_per_order=25_000))
        decision = engine.validate_order(
            order_id=11,
            client_id=1,
            side=0,
            price_float=100.00,
            quantity=30_000,  # > 25,000
        )
        assert decision.passed is False
        assert decision.rule_name == "MAX_QUANTITY"
        assert "MAX_QUANTITY_EXCEEDED" in decision.reject_reason
        assert "30,000" in decision.reject_reason
        assert engine.quantity_blocks == 1

    def test_order_exceeding_max_notional_rejected(self):
        """Order with dollar notional exceeding limit is rejected."""
        engine = PreTradeRiskEngine(RiskLimits(max_notional_per_order=500_000.0, max_quantity_per_order=100_000))
        decision = engine.validate_order(
            order_id=12,
            client_id=1,
            side=0,
            price_float=150.00,
            quantity=5_000,  # Notional: $750,000 > $500,000
        )
        assert decision.passed is False
        assert decision.rule_name == "MAX_NOTIONAL"
        assert "MAX_NOTIONAL_EXCEEDED" in decision.reject_reason
        assert "$750,000.00" in decision.reject_reason
        assert engine.notional_blocks == 1


# ─────────────────────────────────────────────
# 3. TOKEN-BUCKET RATE LIMITER TESTS
# ─────────────────────────────────────────────

class TestRateLimiter:

    def test_burst_within_rate_limit_allowed(self):
        """Orders within rate capacity pass without throttling."""
        limiter = TokenBucketRateLimiter(rate_per_second=100, burst_capacity=100)
        t_now = time.perf_counter_ns()
        for _ in range(50):
            allowed, _ = limiter.check_and_consume(client_id=1, tokens=1.0, timestamp_ns=t_now)
            assert allowed is True

    def test_burst_exceeding_capacity_rejected(self):
        """Orders exceeding burst capacity are rejected."""
        limiter = TokenBucketRateLimiter(rate_per_second=10, burst_capacity=10)
        t_now = time.perf_counter_ns()
        # Consume 10 allowed tokens
        for _ in range(10):
            allowed, _ = limiter.check_and_consume(client_id=2, tokens=1.0, timestamp_ns=t_now)
            assert allowed is True

        # 11th token should be blocked
        allowed, _ = limiter.check_and_consume(client_id=2, tokens=1.0, timestamp_ns=t_now)
        assert allowed is False

    def test_independent_client_rate_limits(self):
        """One client exhausting tokens does not affect another client."""
        limiter = TokenBucketRateLimiter(rate_per_second=5, burst_capacity=5)
        t_now = time.perf_counter_ns()
        for _ in range(5):
            limiter.check_and_consume(client_id=1, tokens=1.0, timestamp_ns=t_now)

        # Client 1 is exhausted
        allowed_1, _ = limiter.check_and_consume(client_id=1, tokens=1.0, timestamp_ns=t_now)
        assert allowed_1 is False

        # Client 2 still has full capacity
        allowed_2, _ = limiter.check_and_consume(client_id=2, tokens=1.0, timestamp_ns=t_now)
        assert allowed_2 is True

    def test_engine_rate_limit_rejection_metrics(self):
        """PreTradeRiskEngine records rate-limit rejections in telemetry."""
        engine = PreTradeRiskEngine(RiskLimits(max_orders_per_second=5))
        t_now = time.perf_counter_ns()
        for _ in range(5):
            res = engine.validate_order(1, client_id=9, side=0, price_float=100.0, quantity=10, timestamp_ns=t_now)
            assert res.passed is True

        blocked_res = engine.validate_order(2, client_id=9, side=0, price_float=100.0, quantity=10, timestamp_ns=t_now)
        assert blocked_res.passed is False
        assert blocked_res.rule_name == "RATE_LIMIT"
        assert "RATE_LIMIT_EXCEEDED" in blocked_res.reject_reason
        assert engine.rate_limit_blocks == 1


# ─────────────────────────────────────────────
# 4. ACCOUNT MARGIN & PURCHASING POWER TESTS
# ─────────────────────────────────────────────

class TestAccountMarginLedger:

    def test_account_initialization_and_properties(self):
        """Verify account balances and available margin calculations."""
        acc = AccountBalance(client_id=1, cash_balance=50_000.0, positions={1: 500})
        assert acc.available_cash == 50_000.0
        assert acc.available_shares(1) == 500
        assert acc.available_shares(2) == 0

    def test_buy_reservation_locks_cash(self):
        """Placing a BUY limit locks cash from available balance."""
        acc = AccountBalance(client_id=1, cash_balance=15_000.0)
        # Buy 100 shares at $100.00 = $10,000 cash needed
        success = acc.reserve_order(order_id=101, side=0, price=100.00, quantity=100, symbol_id=1)
        assert success is True
        assert acc.cash_reserved == 10_000.0
        assert acc.available_cash == 5_000.0

    def test_insufficient_cash_reservation_fails(self):
        """Attempting to buy with insufficient cash fails reservation."""
        acc = AccountBalance(client_id=1, cash_balance=5_000.0)
        success = acc.reserve_order(order_id=102, side=0, price=100.00, quantity=100, symbol_id=1)
        assert success is False
        assert acc.cash_reserved == 0.0

    def test_sell_reservation_locks_shares(self):
        """Placing a SELL limit locks shares from available position."""
        acc = AccountBalance(client_id=1, cash_balance=0.0, positions={1: 200})
        success = acc.reserve_order(order_id=103, side=1, price=150.00, quantity=150, symbol_id=1)
        assert success is True
        assert acc.available_shares(1) == 50
        assert acc.positions_reserved[1] == 150

    def test_insufficient_shares_reservation_fails(self):
        """Attempting to sell more shares than owned fails reservation."""
        acc = AccountBalance(client_id=1, cash_balance=0.0, positions={1: 50})
        success = acc.reserve_order(order_id=104, side=1, price=150.00, quantity=100, symbol_id=1)
        assert success is False

    def test_cancel_order_releases_margin(self):
        """Cancelling a resting order unlocks reserved cash."""
        acc = AccountBalance(client_id=1, cash_balance=20_000.0)
        acc.reserve_order(order_id=201, side=0, price=100.00, quantity=100, symbol_id=1)
        assert acc.available_cash == 10_000.0

        # Cancel unexecuted order
        acc.release_order(order_id=201)
        assert acc.cash_reserved == 0.0
        assert acc.available_cash == 20_000.0

    def test_trade_settlement_updates_cash_and_inventory(self):
        """Trade execution settles cash debits/credits and share positions."""
        acc_buyer = AccountBalance(client_id=1, cash_balance=20_000.0, positions={1: 0})
        acc_seller = AccountBalance(client_id=2, cash_balance=5_000.0, positions={1: 100})

        acc_buyer.reserve_order(order_id=301, side=0, price=150.00, quantity=100, symbol_id=1)
        acc_seller.reserve_order(order_id=302, side=1, price=150.00, quantity=100, symbol_id=1)

        # Execute 100 shares @ $150.00 = $15,000 notional
        acc_buyer.settle_trade(side=0, price=150.00, quantity=100, symbol_id=1, order_id=301)
        acc_seller.settle_trade(side=1, price=150.00, quantity=100, symbol_id=1, order_id=302)

        # Buyer: $20,000 - $15,000 = $5,000 cash; +100 shares; 0 reserved
        assert acc_buyer.cash_balance == 5_000.0
        assert acc_buyer.cash_reserved == 0.0
        assert acc_buyer.positions[1] == 100

        # Seller: $5,000 + $15,000 = $20,000 cash; 0 shares; 0 reserved
        assert acc_seller.cash_balance == 20_000.0
        assert acc_seller.positions[1] == 0
        assert acc_seller.positions_reserved[1] == 0

    def test_engine_enforces_margin_rejection(self):
        """PreTradeRiskEngine blocks orders when enforce_margin=True and funds are insufficient."""
        limits = RiskLimits(enforce_margin=True)
        engine = PreTradeRiskEngine(limits)
        acc = engine.get_or_create_account(client_id=77, initial_cash=1_000.0, initial_shares=10)

        # Try to buy $10,000 worth with only $1,000 cash
        res = engine.validate_order(order_id=401, client_id=77, side=0, price_float=100.0, quantity=100)
        assert res.passed is False
        assert res.rule_name == "INSUFFICIENT_FUNDS"
        assert "INSUFFICIENT_FUNDS" in res.reject_reason
        assert engine.margin_blocks == 1

        # Try to sell 50 shares with only 10 shares
        res_sell = engine.validate_order(order_id=402, client_id=77, side=1, price_float=100.0, quantity=50)
        assert res_sell.passed is False
        assert res_sell.rule_name == "INSUFFICIENT_POSITION"
        assert "INSUFFICIENT_POSITION" in res_sell.reject_reason


# ─────────────────────────────────────────────
# 5. CIRCUIT BREAKER / MARKET HALT TESTS
# ─────────────────────────────────────────────

class TestCircuitBreaker:

    def test_circuit_breaker_halts_trading(self):
        """Active circuit breaker blocks all incoming orders immediately."""
        engine = PreTradeRiskEngine()
        assert engine.limits.circuit_breaker_active is False

        # Trip breaker
        engine.trip_circuit_breaker()
        assert engine.limits.circuit_breaker_active is True

        decision = engine.validate_order(order_id=501, client_id=1, side=0, price_float=150.0, quantity=100)
        assert decision.passed is False
        assert decision.rule_name == "CIRCUIT_BREAKER"
        assert "CIRCUIT_BREAKER_ACTIVE" in decision.reject_reason
        assert engine.circuit_breaker_blocks == 1

    def test_reset_circuit_breaker_resumes_trading(self):
        """Resetting circuit breaker restores normal order flow."""
        engine = PreTradeRiskEngine()
        engine.trip_circuit_breaker()
        engine.reset_circuit_breaker()

        decision = engine.validate_order(order_id=502, client_id=1, side=0, price_float=150.0, quantity=100)
        assert decision.passed is True


# ─────────────────────────────────────────────
# 6. SERVER INTEGRATION TESTS
# ─────────────────────────────────────────────

class TestServerRiskIntegration:

    def test_server_blocks_fat_finger_order(self):
        """WebTradingServer rejects fat-finger orders violating price collar."""
        server = WebTradingServer(db_path=":memory:")
        # Seeded book has midpoint around $150.025; order at $180.00 is ~20% away (> 10% collar)
        res = server.place_order(
            side_str="BUY",
            type_str="LIMIT",
            price_float=180.00,
            quantity=100,
        )
        assert res["success"] is False
        assert res["total_filled"] == 0
        assert "PRICE_COLLAR_BREACH" in res["reject_reason"]

    def test_server_blocks_circuit_breaker(self):
        """WebTradingServer rejects orders when circuit breaker is tripped."""
        server = WebTradingServer(db_path=":memory:")
        server.risk_engine.trip_circuit_breaker()

        res = server.place_order(
            side_str="BUY",
            type_str="LIMIT",
            price_float=150.00,
            quantity=100,
        )
        assert res["success"] is False
        assert "CIRCUIT_BREAKER_ACTIVE" in res["reject_reason"]

    def test_server_snapshot_includes_risk_telemetry(self):
        """get_snapshot() includes risk metrics and account balance data."""
        server = WebTradingServer(db_path=":memory:")
        snap = server.get_snapshot()

        assert "risk" in snap
        assert "circuit_breaker_active" in snap["risk"]
        assert "total_evaluated" in snap["risk"]
        assert "account" in snap
        assert "available_cash" in snap["account"]
        assert "cash_balance" in snap["account"]
