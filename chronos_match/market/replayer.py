"""
ChronosMatch: Real-Market Replay & Adaptive Simulation Engine.

Orchestrates real-world historical market data streaming, micro-tick synthesis,
adaptive ML price impact, and autonomous algorithmic trading.
"""

from dataclasses import dataclass
import random
import time
from typing import Dict, List, Optional, Tuple, Any

from chronos_match.market.data_loader import MarketDataLoader, StockCandle
from chronos_match.market.ml_impact_engine import MLMarketImpactEngine
from chronos_match.algo import AlgoTradingManager
from chronos_match.market.router import MultiBookRouter, SingleSymbolVenue, SymbolInfo
from chronos_match.protocol import to_fixed_price, from_fixed_price, OrderSide, OrderType, TradePacket


class HistoricalMarketReplayer:
    """
    Simulates authentic real-market conditions using real historical stock datasets,
    reactive ML price impact, and high-frequency order book replenishment.
    """

    def __init__(
        self,
        router: MultiBookRouter,
        data_loader: Optional[MarketDataLoader] = None,
        default_ticker: str = "NVDA",
    ):
        self.router = router
        self.data_loader = data_loader or MarketDataLoader()
        self.active_ticker = default_ticker.upper()

        self.impact_engines: Dict[str, MLMarketImpactEngine] = {}
        self.algo_managers: Dict[str, AlgoTradingManager] = {}
        self.candles_cache: Dict[str, List[StockCandle]] = {}
        self.current_candle_idx: Dict[str, int] = {}

        # Initialize default stock
        self.bind_stock(self.active_ticker)

    def bind_stock(self, ticker: str) -> SingleSymbolVenue:
        """
        Bind or dynamically register a real historical stock into the exchange router.
        """
        sym = ticker.strip().upper()
        self.active_ticker = sym

        # Load historical candles & profile
        try:
            candles = self.data_loader.load_candles(sym, max_bars=300)
            profile = self.data_loader.compute_profile(sym, lookback=60)
            initial_p = profile["latest_price"]
            daily_vol = profile["volatility_daily"]
            avg_vol = profile["avg_volume"]
            tick_size = profile["tick_size"]
        except Exception:
            # Fallback for synthetic/unindexed symbols
            candles = []
            initial_p = 150.00
            daily_vol = 0.02
            avg_vol = 500_000
            tick_size = 0.01

        self.candles_cache[sym] = candles
        self.current_candle_idx[sym] = len(candles) - 1 if candles else 0

        # Create or retrieve venue
        try:
            venue = self.router.get_venue(sym)
            venue.info.initial_price = initial_p
            venue.last_trade_price = initial_p
        except KeyError:
            # Register new symbol in router
            sym_id = len(self.router.venues_by_id) + 1
            info = SymbolInfo(
                symbol_id=sym_id,
                ticker=sym,
                base_asset=sym.split("-")[0],
                quote_asset="USD" if "BSE" not in sym else "INR",
                tick_size=tick_size,
                lot_size=1,
                initial_price=initial_p,
            )
            venue = self.router.register_symbol(info, seed=True)

        # Initialize dedicated ML Impact Engine for this instrument
        if sym not in self.impact_engines:
            self.impact_engines[sym] = MLMarketImpactEngine(
                base_price=initial_p,
                daily_volatility=daily_vol,
                avg_daily_volume=avg_vol,
            )
        else:
            self.impact_engines[sym].set_baseline_price(initial_p)

        # Map venue ticker alias if different
        self.impact_engines[venue.info.ticker] = self.impact_engines[sym]

        # Initialize Algorithmic Trading Suite for this instrument
        if sym not in self.algo_managers:
            self.algo_managers[sym] = AlgoTradingManager(symbol=sym)
        self.algo_managers[venue.info.ticker] = self.algo_managers[sym]

        return venue

    def step_market_tick(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Advance one simulation step:
        1. Computes effective price from macro historical anchor + ML order flow impact.
        2. Adjusts market making quotes.
        3. Evaluates momentum breakout signals.
        4. Generates authentic background liquidity and crossing flow.
        """
        sym = (symbol or self.active_ticker).upper()
        venue = self.router.get_venue(sym)
        impact = self.impact_engines.get(sym)
        algo_mgr = self.algo_managers.get(sym)

        if not impact:
            return []

        # 1. Update Macro Anchor from historical candles
        candles = self.candles_cache.get(sym, [])
        if candles:
            # Drift slightly based on current candle range
            idx = self.current_candle_idx.get(sym, len(candles) - 1)
            c = candles[idx]
            impact.set_baseline_price(c.close)

        effective_mid = impact.get_effective_price()
        tick = venue.info.tick_size
        events: List[Dict[str, Any]] = []

        # 2. Market Maker Quoting
        if algo_mgr and algo_mgr.market_maker.active:
            res_p, bid_p, ask_p = algo_mgr.market_maker.compute_quotes(
                mid_price=effective_mid,
                volatility=impact.daily_volatility,
                impact_engine=impact,
            )
            algo_mgr.market_maker.total_quotes_placed += 2

            # Place resting bid quote on book
            bid_oid = self.router.allocate_order_id()
            self.router.client_order_registry[bid_oid] = algo_mgr.market_maker.bot_id
            venue.book.add_limit_order(
                order_id=bid_oid,
                client_id=algo_mgr.market_maker.bot_id,
                price=to_fixed_price(bid_p),
                quantity=algo_mgr.market_maker.order_size,
                side=int(OrderSide.BUY),
                symbol_id=venue.info.symbol_id,
            )
            venue.total_orders += 1

            # Place resting ask quote on book
            ask_oid = self.router.allocate_order_id()
            self.router.client_order_registry[ask_oid] = algo_mgr.market_maker.bot_id
            venue.book.add_limit_order(
                order_id=ask_oid,
                client_id=algo_mgr.market_maker.bot_id,
                price=to_fixed_price(ask_p),
                quantity=algo_mgr.market_maker.order_size,
                side=int(OrderSide.SELL),
                symbol_id=venue.info.symbol_id,
            )
            venue.total_orders += 1

        # 3. Momentum Bot Evaluation
        if algo_mgr and algo_mgr.momentum_bot.active:
            sig = algo_mgr.momentum_bot.update_tick(
                price=effective_mid,
                order_flow_imbalance=impact.ofi,
            )
            if sig:
                action, qty, px = sig
                side_enum = OrderSide.BUY if action == "BUY" else OrderSide.SELL
                mom_oid = self.router.allocate_order_id()
                self.router.client_order_registry[mom_oid] = algo_mgr.momentum_bot.bot_id

                # Execute aggressive taker limit/market order
                matches = venue.book.add_limit_order(
                    order_id=mom_oid,
                    client_id=algo_mgr.momentum_bot.bot_id,
                    price=to_fixed_price(px * (1.002 if action == "BUY" else 0.998)),
                    quantity=qty,
                    side=int(side_enum),
                    symbol_id=venue.info.symbol_id,
                )
                for m in matches:
                    trade_p = from_fixed_price(m["execution_price"])
                    impact.record_execution(int(side_enum), m["quantity"], trade_p)
                    algo_mgr.on_trade_executed(m["buy_order_id"], int(OrderSide.BUY), trade_p, m["quantity"])
                    algo_mgr.on_trade_executed(m["sell_order_id"], int(OrderSide.SELL), trade_p, m["quantity"])
                    events.append({"type": "MOMENTUM_TRADE", "side": action, "price": trade_p, "qty": m["quantity"]})

        # 4. Realistic Natural Market Noise & Liquidity Replenishment
        # Occasionally place resting limit order or execute small retail taker trade
        roll = random.random()
        spread_mult = impact.get_spread_multiplier()

        if roll < 0.60:
            # Place resting liquidity around effective mid
            side = OrderSide.BUY if random.random() < 0.50 else OrderSide.SELL
            offset = random.choice([1, 2, 3, 5]) * tick * spread_mult
            order_p = (effective_mid - offset) if side == OrderSide.BUY else (effective_mid + offset)
            qty = random.choice([50, 100, 200, 500])
            oid = self.router.allocate_order_id()
            self.router.client_order_registry[oid] = 99  # Retail / Liquidity provider client

            matches = venue.book.add_limit_order(
                order_id=oid,
                client_id=99,
                price=to_fixed_price(round(order_p, 2)),
                quantity=qty,
                side=int(side),
                symbol_id=venue.info.symbol_id,
            )
            venue.total_orders += 1

            # If matches occur, feed into ML Impact Model!
            for m in matches:
                trade_p = from_fixed_price(m["execution_price"])
                venue.last_trade_price = trade_p
                venue.total_trades += 1
                venue.total_volume += m["quantity"]
                venue.high_price = max(venue.high_price, trade_p)
                venue.low_price = min(venue.low_price, trade_p)
                # ML Model shifts market on trades!
                impact.record_execution(int(side), m["quantity"], trade_p)

                if algo_mgr:
                    algo_mgr.on_trade_executed(m["buy_order_id"], int(OrderSide.BUY), trade_p, m["quantity"])
                    algo_mgr.on_trade_executed(m["sell_order_id"], int(OrderSide.SELL), trade_p, m["quantity"])

                events.append({"type": "NATURAL_FILL", "price": trade_p, "qty": m["quantity"]})

        return events

    def get_candlesticks(self, symbol: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Return historical candlestick list for active or specified symbol."""
        sym = (symbol or self.active_ticker).upper()
        candles = self.candles_cache.get(sym, [])
        if not candles:
            # Try loading
            candles = self.data_loader.load_candles(sym, max_bars=limit)
            self.candles_cache[sym] = candles

        return [
            {
                "date": c.date,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles[-limit:]
        ]
