"""
ChronosMatch: Test Suite for Phase 3 — Multi-Symbol & Multi-Asset Matching Router.

Tests cover:
  - Symbol Registry & Metadata Resolution (by int ID and ticker string)
  - Isolated Order Books per Instrument (no cross-asset book pollution)
  - Multi-Asset Clearinghouse Ledger (Cash USD + BTC, ETH, SOL positions)
  - Symbol-Specific Price Collars & Fat-Finger Rules
  - Multi-Book Router 24h Tickers & Aggregate Snapshots
  - Web Server Multi-Symbol Order Placement & Switching
"""

import pytest
import time

from chronos_match.market.router import (
    SymbolInfo,
    SingleSymbolVenue,
    MultiBookRouter,
)
from chronos_match.protocol import to_fixed_price, from_fixed_price, OrderSide
from chronos_match.risk.risk_engine import PreTradeRiskEngine, RiskLimits
from chronos_match.web.server import WebTradingServer


# ─────────────────────────────────────────────
# 1. SYMBOL REGISTRY & RESOLUTION TESTS
# ─────────────────────────────────────────────

class TestSymbolRegistry:

    def test_symbol_registration_and_lookup(self):
        """Register multiple symbols and verify resolution by ID and ticker string."""
        router = MultiBookRouter()
        sol = router.register_symbol(SymbolInfo(1, "SOL-USD", "SOL", "USD", 0.01, 1, 150.00))
        btc = router.register_symbol(SymbolInfo(2, "BTC-USD", "BTC", "USD", 0.50, 1, 65000.00))
        eth = router.register_symbol(SymbolInfo(3, "ETH-USD", "ETH", "USD", 0.10, 1, 3500.00))

        # Lookup by integer symbol_id
        assert router.get_venue(1).info.ticker == "SOL-USD"
        assert router.get_venue(2).info.ticker == "BTC-USD"
        assert router.get_venue(3).info.ticker == "ETH-USD"

        # Lookup by exact ticker string
        assert router.get_venue("BTC-USD").info.symbol_id == 2
        assert router.get_venue("ETH-USD").info.symbol_id == 3

        # Lookup by case-insensitive / slash-separated ticker
        assert router.get_venue("btc-usd").info.symbol_id == 2
        assert router.get_venue("BTC/USD").info.symbol_id == 2
        assert router.get_venue("ETH").info.symbol_id == 3

    def test_unknown_symbol_raises_keyerror(self):
        """Querying an unregistered symbol raises KeyError with clear message."""
        router = MultiBookRouter()
        router.register_symbol(SymbolInfo(1, "SOL-USD", "SOL", "USD", 0.01, 1, 150.00))

        with pytest.raises(KeyError) as exc_info:
            router.get_venue("DOGE-USD")
        assert "DOGE-USD" in str(exc_info.value)

    def test_list_symbols_metadata(self):
        """list_symbols() returns full list of registered instruments."""
        router = MultiBookRouter()
        router.register_symbol(SymbolInfo(1, "SOL-USD", "SOL", "USD", 0.01, 1, 150.00))
        router.register_symbol(SymbolInfo(2, "BTC-USD", "BTC", "USD", 0.50, 1, 65000.00))

        symbols = router.list_symbols()
        assert len(symbols) == 2
        assert symbols[0]["ticker"] == "SOL-USD"
        assert symbols[1]["ticker"] == "BTC-USD"
        assert symbols[1]["tick_size"] == 0.50


# ─────────────────────────────────────────────
# 2. ISOLATED MATCHING VENUES TESTS
# ─────────────────────────────────────────────

class TestOrderBookIsolation:

    def test_independent_order_books_no_cross_pollution(self):
        """Trading activity in BTC-USD does not alter or pollute SOL-USD order book."""
        router = MultiBookRouter()
        sol_venue = router.register_symbol(SymbolInfo(1, "SOL-USD", "SOL", "USD", 0.01, 1, 150.00))
        btc_venue = router.register_symbol(SymbolInfo(2, "BTC-USD", "BTC", "USD", 0.50, 1, 65000.00))

        sol_bid_before = sol_venue.book.best_bid_price
        sol_ask_before = sol_venue.book.best_ask_price

        # Place limit buy inside spread in BTC-USD (spread is 64999.50 to 65000.50)
        btc_venue.book.add_limit_order(
            order_id=5001,
            client_id=1,
            price=to_fixed_price(65000.00),
            quantity=10,
            side=int(OrderSide.BUY),
            symbol_id=2,
            timestamp_ns=time.perf_counter_ns(),
        )

        # SOL-USD book remains completely unchanged
        assert sol_venue.book.best_bid_price == sol_bid_before
        assert sol_venue.book.best_ask_price == sol_ask_before

        # BTC-USD reflects its own new top-of-book
        assert from_fixed_price(btc_venue.book.best_bid_price) == 65000.00

    def test_trade_execution_in_one_symbol_increments_only_that_venue(self):
        """Matched trades increment volume and counters only on the active venue."""
        router = MultiBookRouter()
        sol_venue = router.register_symbol(SymbolInfo(1, "SOL-USD", "SOL", "USD", 0.01, 1, 150.00))
        eth_venue = router.register_symbol(SymbolInfo(3, "ETH-USD", "ETH", "USD", 0.10, 1, 3500.00))

        sol_trades_before = sol_venue.total_trades
        eth_trades_before = eth_venue.total_trades

        # Place crossing orders in ETH-USD
        eth_venue.book.add_limit_order(
            order_id=6001,
            client_id=10,
            price=to_fixed_price(3500.00),
            quantity=5,
            side=int(OrderSide.SELL),
            symbol_id=3,
        )
        matches = eth_venue.book.add_limit_order(
            order_id=6002,
            client_id=20,
            price=to_fixed_price(3500.00),
            quantity=5,
            side=int(OrderSide.BUY),
            symbol_id=3,
        )
        assert len(matches) > 0

        # SOL-USD trade count is completely unaffected
        assert sol_venue.total_trades == sol_trades_before


# ─────────────────────────────────────────────
# 3. MULTI-ASSET CLEARINGHOUSE LEDGER TESTS
# ─────────────────────────────────────────────

class TestMultiAssetClearinghouse:

    def test_multi_asset_portfolio_balances(self):
        """Account holds separate inventory positions for BTC, ETH, and SOL."""
        engine = PreTradeRiskEngine(RiskLimits(enforce_margin=True))
        account = engine.get_or_create_account(
            client_id=5,
            initial_cash=500_000.0,
            initial_shares=100,  # symbol 1: SOL
            symbol_id=1,
        )
        # Deposit ETH (symbol 3) and BTC (symbol 2)
        account.deposit(shares=50, symbol_id=3)  # 50 ETH
        account.deposit(shares=10, symbol_id=2)  # 10 BTC

        assert account.available_cash == 500_000.0
        assert account.available_shares(symbol_id=1) == 100  # SOL
        assert account.available_shares(symbol_id=2) == 10   # BTC
        assert account.available_shares(symbol_id=3) == 50   # ETH

    def test_multi_asset_settlement_updates_correct_inventory(self):
        """Executing trades in different assets updates the corresponding inventory."""
        engine = PreTradeRiskEngine(RiskLimits(enforce_margin=True))
        buyer = engine.get_or_create_account(client_id=1, initial_cash=200_000.0, initial_shares=0)
        seller = engine.get_or_create_account(client_id=2, initial_cash=0.0, initial_shares=0)

        # Give seller 2 BTC (symbol 2) and 20 ETH (symbol 3)
        seller.deposit(shares=2, symbol_id=2)
        seller.deposit(shares=20, symbol_id=3)

        # Buyer buys 1 BTC @ $65,000.00
        engine.on_trade_executed(
            buy_client_id=1,
            sell_client_id=2,
            price_float=65000.00,
            quantity=1,
            symbol_id=2,
        )

        assert buyer.cash_balance == 135_000.0  # 200,000 - 65,000
        assert buyer.positions[2] == 1          # 1 BTC owned
        assert seller.cash_balance == 65_000.0
        assert seller.positions[2] == 1         # 1 BTC left

        # Buyer now buys 10 ETH @ $3,500.00 = $35,000
        engine.on_trade_executed(
            buy_client_id=1,
            sell_client_id=2,
            price_float=3500.00,
            quantity=10,
            symbol_id=3,
        )

        assert buyer.cash_balance == 100_000.0  # 135,000 - 35,000
        assert buyer.positions[2] == 1          # BTC untouched
        assert buyer.positions[3] == 10         # 10 ETH owned

    def test_cannot_cross_asset_oversell(self):
        """Holding BTC does not allow selling ETH if ETH balance is 0."""
        engine = PreTradeRiskEngine(RiskLimits(enforce_margin=True))
        acc = engine.get_or_create_account(client_id=9, initial_cash=100_000.0, initial_shares=5, symbol_id=2)

        # Try selling 5 ETH (symbol 3) while only holding BTC (symbol 2)
        decision = engine.validate_order(
            order_id=7001,
            client_id=9,
            side=1,  # SELL
            price_float=3500.00,
            quantity=5,
            symbol_id=3,  # ETH
        )
        assert decision.passed is False
        assert decision.rule_name == "INSUFFICIENT_POSITION"


# ─────────────────────────────────────────────
# 4. SYMBOL-SPECIFIC RISK COLLARS TESTS
# ─────────────────────────────────────────────

class TestSymbolSpecificRiskCollars:

    def test_btc_price_collar_relative_to_btc_mid(self):
        """BTC-USD order at $66,000 passes 10% collar; $75,000 is rejected."""
        engine = PreTradeRiskEngine(RiskLimits(price_collar_pct=0.10))

        # Best bid: 64,950, Best ask: 65,050 -> mid: 65,000
        # 10% collar bounds: $58,500 - $71,500

        # $66,000 is 1.5% away -> PASS
        d_pass = engine.validate_order(
            order_id=8001,
            client_id=1,
            side=0,
            price_float=66000.00,
            quantity=1,
            symbol_id=2,
            best_bid_float=64950.00,
            best_ask_float=65050.00,
        )
        assert d_pass.passed is True

        # $75,000 is 15.3% away -> REJECT
        d_fail = engine.validate_order(
            order_id=8002,
            client_id=1,
            side=0,
            price_float=75000.00,
            quantity=1,
            symbol_id=2,
            best_bid_float=64950.00,
            best_ask_float=65050.00,
        )
        assert d_fail.passed is False
        assert "PRICE_COLLAR_BREACH" in d_fail.reject_reason


# ─────────────────────────────────────────────
# 5. MULTI-BOOK ROUTER TICKERS & AGGREGATE
# ─────────────────────────────────────────────

class TestRouterAggregates:

    def test_get_all_tickers_returns_all_venues(self):
        """get_all_tickers() returns 24h market metrics for every registered symbol."""
        router = MultiBookRouter()
        router.register_symbol(SymbolInfo(1, "SOL-USD", "SOL", "USD", 0.01, 1, 150.00))
        router.register_symbol(SymbolInfo(2, "BTC-USD", "BTC", "USD", 0.50, 1, 65000.00))
        router.register_symbol(SymbolInfo(3, "ETH-USD", "ETH", "USD", 0.10, 1, 3500.00))

        tickers = router.get_all_tickers()
        assert len(tickers) == 3

        ticker_names = [t["symbol"] for t in tickers]
        assert "SOL-USD" in ticker_names
        assert "BTC-USD" in ticker_names
        assert "ETH-USD" in ticker_names

        btc_t = [t for t in tickers if t["symbol"] == "BTC-USD"][0]
        assert btc_t["last_price"] > 0
        assert btc_t["best_bid"] > 0
        assert btc_t["best_ask"] > 0


# ─────────────────────────────────────────────
# 6. WEB SERVER MULTI-SYMBOL INTEGRATION TESTS
# ─────────────────────────────────────────────

class TestServerMultiSymbolIntegration:

    def test_server_places_order_on_specific_symbol(self):
        """WebTradingServer executes orders routed to different symbols."""
        server = WebTradingServer(db_path=":memory:")

        # Place order on SOL-USD (default)
        res_sol = server.place_order(
            side_str="BUY",
            type_str="LIMIT",
            price_float=148.00,
            quantity=100,
            symbol="SOL-USD",
        )
        assert res_sol["success"] is True
        assert res_sol["symbol"] == "SOL-USD"

        # Place order on BTC-USD
        res_btc = server.place_order(
            side_str="BUY",
            type_str="LIMIT",
            price_float=64500.00,
            quantity=1,
            symbol="BTC-USD",
        )
        assert res_btc["success"] is True
        assert res_btc["symbol"] == "BTC-USD"

        # Verify snapshots are segregated
        snap_sol = server.get_snapshot(symbol="SOL-USD")
        snap_btc = server.get_snapshot(symbol="BTC-USD")

        assert snap_sol["symbol"] == "SOL-USD"
        assert snap_btc["symbol"] == "BTC-USD"
        assert snap_btc["best_bid"] > 50000.0

    def test_server_list_symbols_and_tickers_endpoints(self):
        """Server exposes registered symbols and aggregate ticker list."""
        server = WebTradingServer(db_path=":memory:")

        symbols = server.router.list_symbols()
        assert len(symbols) >= 3
        tickers = [s["ticker"] for s in symbols]
        assert "SOL-USD" in tickers
        assert "BTC-USD" in tickers
        assert "ETH-USD" in tickers
