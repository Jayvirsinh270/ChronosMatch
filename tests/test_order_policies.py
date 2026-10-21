"""
ChronosMatch: Test Suite for Phase 1 — Advanced Order Types.

Tests cover:
  - IOC (Immediate-or-Cancel): partial fills, full fills, empty book.
  - FOK (Fill-or-Kill): full fill success, insufficient liquidity, empty book.
  - Post-Only / Maker-Only: passive resting, crossing-spread rejection.
  - STP (Self-Trade Prevention): same-client detection, different-client passthrough.
  - Combined scenarios: IOC + STP, response payload field coverage.
"""

import pytest
import time

from chronos_match.protocol import to_fixed_price, OrderSide
from chronos_match.engine.order_book_cy import FastOrderBook
from chronos_match.engine.order_policies import (
    OrderPolicyEngine,
    TimeInForce,
    SelfTradeAction,
)
from chronos_match.web.server import WebTradingServer


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def make_engine(arena_capacity: int = 10_000):
    """Create a fresh FastOrderBook + OrderPolicyEngine pair."""
    book = FastOrderBook(arena_capacity=arena_capacity)
    registry = {}
    engine = OrderPolicyEngine(book=book, client_order_registry=registry)
    return book, engine


def seed_asks(book, engine, base_price: float = 150.05, levels: int = 5, qty_per_level: int = 500):
    """Place resting SELL orders so there is always ask-side liquidity."""
    oid = 9000
    for i in range(levels):
        p = to_fixed_price(base_price + i * 0.05)
        book.add_limit_order(
            order_id=oid + i,
            client_id=99,
            price=p,
            quantity=qty_per_level,
            side=int(OrderSide.SELL),
            timestamp_ns=time.perf_counter_ns(),
        )
        engine.register_order(oid + i, 99)
    return oid


def seed_bids(book, engine, base_price: float = 149.95, levels: int = 5, qty_per_level: int = 500):
    """Place resting BUY orders so there is always bid-side liquidity."""
    oid = 8000
    for i in range(levels):
        p = to_fixed_price(base_price - i * 0.05)
        book.add_limit_order(
            order_id=oid + i,
            client_id=99,
            price=p,
            quantity=qty_per_level,
            side=int(OrderSide.BUY),
            timestamp_ns=time.perf_counter_ns(),
        )
        engine.register_order(oid + i, 99)
    return oid


# ─────────────────────────────────────────────
# IOC TESTS
# ─────────────────────────────────────────────

class TestIOC:

    def test_ioc_partial_fill_cancels_remainder(self):
        """IOC: only 500 shares are resting; requesting 1200 → fills 500, cancels 700."""
        book, engine = make_engine()
        seed_asks(book, engine, base_price=150.05, levels=1, qty_per_level=500)

        result = engine.apply(
            order_id=1,
            client_id=1,
            price_fixed=to_fixed_price(150.10),  # Aggressive enough to cross
            quantity=1200,
            side=int(OrderSide.BUY),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            tif=TimeInForce.IOC,
        )

        assert result.accepted is True
        assert result.total_filled == 500
        assert result.cancelled_remainder == 700
        assert result.tif == TimeInForce.IOC
        # Verify the remainder is NOT resting on the book (IOC cleaned it up)
        snapshot = book.get_l2_snapshot(depth=5)
        for bid in snapshot.get("bids", []):
            assert bid["price"] != to_fixed_price(150.10), "IOC remainder must not rest on book"

    def test_ioc_full_fill_no_cancel(self):
        """IOC: 2500 shares resting, requesting only 500 → full fill, no cancel."""
        book, engine = make_engine()
        seed_asks(book, engine, base_price=150.05, levels=5, qty_per_level=500)

        result = engine.apply(
            order_id=2,
            client_id=1,
            price_fixed=to_fixed_price(150.30),
            quantity=500,
            side=int(OrderSide.BUY),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            tif=TimeInForce.IOC,
        )

        assert result.accepted is True
        assert result.total_filled == 500
        assert result.cancelled_remainder == 0

    def test_ioc_empty_book_full_cancel(self):
        """IOC: empty opposing book → zero fills, entire qty immediately cancelled."""
        book, engine = make_engine()
        # No asks seeded; BUY IOC → nothing to match against

        result = engine.apply(
            order_id=3,
            client_id=1,
            price_fixed=to_fixed_price(150.10),
            quantity=300,
            side=int(OrderSide.BUY),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            tif=TimeInForce.IOC,
        )

        assert result.accepted is True
        assert result.total_filled == 0
        assert result.cancelled_remainder == 300


# ─────────────────────────────────────────────
# FOK TESTS
# ─────────────────────────────────────────────

class TestFOK:

    def test_fok_full_fill_success(self):
        """FOK: 2500 shares resting, requesting 400 → full fill accepted."""
        book, engine = make_engine()
        seed_asks(book, engine, levels=5, qty_per_level=500)

        result = engine.apply(
            order_id=10,
            client_id=1,
            price_fixed=to_fixed_price(150.30),
            quantity=400,
            side=int(OrderSide.BUY),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            tif=TimeInForce.FOK,
        )

        assert result.accepted is True
        assert result.total_filled == 400
        assert result.cancelled_remainder == 0
        assert result.reject_reason is None

    def test_fok_insufficient_liquidity_rejected(self):
        """FOK: only 500 shares available, requesting 1500 → pre-match rejection."""
        book, engine = make_engine()
        seed_asks(book, engine, levels=1, qty_per_level=500)

        result = engine.apply(
            order_id=11,
            client_id=1,
            price_fixed=to_fixed_price(150.10),
            quantity=1500,
            side=int(OrderSide.BUY),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            tif=TimeInForce.FOK,
        )

        assert result.accepted is False
        assert result.total_filled == 0
        assert "FOK_REJECTED" in result.reject_reason
        assert "1,500" in result.reject_reason or "1500" in result.reject_reason

    def test_fok_empty_book_rejected(self):
        """FOK: completely empty opposing book → instant rejection, zero fills."""
        book, engine = make_engine()

        result = engine.apply(
            order_id=12,
            client_id=1,
            price_fixed=to_fixed_price(150.05),
            quantity=100,
            side=int(OrderSide.BUY),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            tif=TimeInForce.FOK,
        )

        assert result.accepted is False
        assert result.total_filled == 0
        assert result.reject_reason is not None

    def test_fok_price_below_best_ask_rejected(self):
        """FOK: limit price below resting ask → no liquidity matchable at that price."""
        book, engine = make_engine()
        seed_asks(book, engine, base_price=150.50, levels=3, qty_per_level=500)

        result = engine.apply(
            order_id=13,
            client_id=1,
            price_fixed=to_fixed_price(150.10),  # Below resting asks at $150.50+
            quantity=300,
            side=int(OrderSide.BUY),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            tif=TimeInForce.FOK,
        )

        # No ask is at or below $150.10, so FOK is rejected
        assert result.accepted is False
        assert result.total_filled == 0


# ─────────────────────────────────────────────
# POST-ONLY / MAKER-ONLY TESTS
# ─────────────────────────────────────────────

class TestPostOnly:

    def test_post_only_passive_buy_rests_on_book(self):
        """POST_ONLY BUY: price is below best ask → accepted as maker, rests on book."""
        book, engine = make_engine()
        seed_asks(book, engine, base_price=150.10, levels=3, qty_per_level=500)
        seed_bids(book, engine, base_price=149.90, levels=3, qty_per_level=500)

        # Place a passive Buy well below the ask; it should rest, not cross
        result = engine.apply(
            order_id=20,
            client_id=1,
            price_fixed=to_fixed_price(149.80),  # Below best ask of $150.10
            quantity=200,
            side=int(OrderSide.BUY),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            post_only=True,
        )

        assert result.accepted is True
        assert result.reject_reason is None
        # No fills — it rests as a maker
        assert result.total_filled == 0

    def test_post_only_crossing_buy_rejected(self):
        """POST_ONLY BUY: price >= best ask → rejected before touching engine."""
        book, engine = make_engine()
        seed_asks(book, engine, base_price=150.05, levels=3, qty_per_level=500)

        result = engine.apply(
            order_id=21,
            client_id=1,
            price_fixed=to_fixed_price(150.10),  # Crosses the ask at $150.05
            quantity=200,
            side=int(OrderSide.BUY),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            post_only=True,
        )

        assert result.accepted is False
        assert "POST_ONLY_REJECTED" in result.reject_reason
        assert result.total_filled == 0

    def test_post_only_passive_sell_rests_on_book(self):
        """POST_ONLY SELL: price above best bid → accepted as maker."""
        book, engine = make_engine()
        seed_bids(book, engine, base_price=149.95, levels=3, qty_per_level=500)

        result = engine.apply(
            order_id=22,
            client_id=1,
            price_fixed=to_fixed_price(150.20),  # Above best bid of $149.95
            quantity=200,
            side=int(OrderSide.SELL),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            post_only=True,
        )

        assert result.accepted is True
        assert result.total_filled == 0
        assert result.reject_reason is None

    def test_post_only_crossing_sell_rejected(self):
        """POST_ONLY SELL: price <= best bid → rejected before touching engine."""
        book, engine = make_engine()
        seed_bids(book, engine, base_price=149.95, levels=3, qty_per_level=500)

        result = engine.apply(
            order_id=23,
            client_id=1,
            price_fixed=to_fixed_price(149.90),  # At or below best bid of $149.95
            quantity=200,
            side=int(OrderSide.SELL),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            post_only=True,
        )

        assert result.accepted is False
        assert "POST_ONLY_REJECTED" in result.reject_reason


# ─────────────────────────────────────────────
# SELF-TRADE PREVENTION TESTS
# ─────────────────────────────────────────────

class TestSTP:

    def test_stp_same_client_cancel_aggressor(self):
        """STP: client_id=5 tries to hit their own resting ask → aggressor cancelled."""
        book, engine = make_engine()

        # Client 5 places a resting ASK at $150.05
        book.add_limit_order(
            order_id=500,
            client_id=5,
            price=to_fixed_price(150.05),
            quantity=300,
            side=int(OrderSide.SELL),
            timestamp_ns=time.perf_counter_ns(),
        )
        engine.register_order(500, 5)

        # Client 5 now sends an aggressive BUY at $150.10 → would hit own order
        result = engine.apply(
            order_id=501,
            client_id=5,
            price_fixed=to_fixed_price(150.10),
            quantity=300,
            side=int(OrderSide.BUY),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            stp_action=SelfTradeAction.CANCEL_AGGRESSOR,
        )

        assert result.accepted is True  # Order was accepted but STP filtered the fill
        assert result.stp_cancelled_qty == 300   # STP voided the self-trade
        assert result.total_filled == 0          # No clean fills through

    def test_stp_different_clients_allowed(self):
        """STP: client_id=5 hits resting order from client_id=7 → trade allowed."""
        book, engine = make_engine()

        # Client 7 places a resting ASK at $150.05
        book.add_limit_order(
            order_id=700,
            client_id=7,
            price=to_fixed_price(150.05),
            quantity=300,
            side=int(OrderSide.SELL),
            timestamp_ns=time.perf_counter_ns(),
        )
        engine.register_order(700, 7)

        # Client 5 sends an aggressive BUY — should trade normally
        result = engine.apply(
            order_id=501,
            client_id=5,
            price_fixed=to_fixed_price(150.10),
            quantity=300,
            side=int(OrderSide.BUY),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            stp_action=SelfTradeAction.CANCEL_AGGRESSOR,
        )

        assert result.accepted is True
        assert result.total_filled == 300
        assert result.stp_cancelled_qty == 0

    def test_stp_allow_mode_permits_self_trade(self):
        """STP ALLOW mode: same-client self-trade is deliberately permitted."""
        book, engine = make_engine()

        book.add_limit_order(
            order_id=600,
            client_id=3,
            price=to_fixed_price(150.05),
            quantity=200,
            side=int(OrderSide.SELL),
            timestamp_ns=time.perf_counter_ns(),
        )
        engine.register_order(600, 3)

        result = engine.apply(
            order_id=601,
            client_id=3,
            price_fixed=to_fixed_price(150.10),
            quantity=200,
            side=int(OrderSide.BUY),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            stp_action=SelfTradeAction.ALLOW,
        )

        # With ALLOW, no STP filter runs — self-trade goes through
        assert result.accepted is True
        assert result.stp_cancelled_qty == 0
        assert result.total_filled == 200


# ─────────────────────────────────────────────
# COMBINED SCENARIO TESTS
# ─────────────────────────────────────────────

class TestCombined:

    def test_ioc_with_stp_fires(self):
        """IOC + STP: self-trade voided, IOC then cancels any leftover remainder."""
        book, engine = make_engine()

        # Client 4 places a resting ASK
        book.add_limit_order(
            order_id=400,
            client_id=4,
            price=to_fixed_price(150.05),
            quantity=300,
            side=int(OrderSide.SELL),
            timestamp_ns=time.perf_counter_ns(),
        )
        engine.register_order(400, 4)

        # Client 4 sends an IOC BUY crossing their own ask — STP should cancel self-trade,
        # and IOC should then cancel the unfilled remainder
        result = engine.apply(
            order_id=401,
            client_id=4,
            price_fixed=to_fixed_price(150.10),
            quantity=300,
            side=int(OrderSide.BUY),
            symbol_id=1,
            timestamp_ns=time.perf_counter_ns(),
            tif=TimeInForce.IOC,
            stp_action=SelfTradeAction.CANCEL_AGGRESSOR,
        )

        assert result.accepted is True
        # STP cancelled the self-trade leg
        assert result.stp_cancelled_qty == 300
        # IOC cancelled the unfilled remainder
        assert result.total_filled == 0

    def test_order_response_fields_complete(self):
        """All Phase 1 response fields must be present in API response dict."""
        server = WebTradingServer(db_path=":memory:")

        # Simple resting GTC limit order
        res = server.place_order(
            side_str="BUY",
            type_str="LIMIT",
            price_float=140.00,
            quantity=100,
            tif_str="GTC",
            post_only=False,
            stp_str="CANCEL_AGGRESSOR",
        )

        required_fields = [
            "success", "order_id", "side", "type",
            "tif", "post_only", "stp",
            "price", "quantity", "total_filled",
            "matched_trades", "cancelled_remainder",
            "stp_cancelled_qty", "reject_reason", "latency_us",
        ]
        for field in required_fields:
            assert field in res, f"Missing field '{field}' in API response"

        assert res["tif"] == "GTC"
        assert res["post_only"] is False
        assert res["stp"] == "CANCEL_AGGRESSOR"
        assert res["success"] is True

    def test_fok_rejection_response_fields(self):
        """FOK rejection response must also include all Phase 1 fields."""
        server = WebTradingServer(db_path=":memory:")

        # FOK with absurdly large quantity → guaranteed rejection
        res = server.place_order(
            side_str="BUY",
            type_str="LIMIT",
            price_float=150.10,
            quantity=9_999_999,
            tif_str="FOK",
            post_only=False,
            stp_str="CANCEL_AGGRESSOR",
        )

        assert res["success"] is False
        assert res["tif"] == "FOK"
        assert res["total_filled"] == 0
        assert res["reject_reason"] is not None
        assert "FOK" in res["reject_reason"]

    def test_post_only_rejection_via_server(self):
        """POST_ONLY rejection via WebTradingServer.place_order() — end-to-end."""
        server = WebTradingServer(db_path=":memory:")
        # Seeded book has best ask around $150.05; placing post_only BUY at $150.10 should reject
        res = server.place_order(
            side_str="BUY",
            type_str="LIMIT",
            price_float=150.10,
            quantity=100,
            tif_str="GTC",
            post_only=True,
            stp_str="CANCEL_AGGRESSOR",
        )

        assert res["success"] is False
        assert res["post_only"] is True
        assert "POST_ONLY_REJECTED" in res["reject_reason"]
