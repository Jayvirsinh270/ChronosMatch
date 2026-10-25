"""
ChronosMatch: Real-Market Replay & Adaptive Simulation Engine.

Orchestrates real-world historical market data streaming, micro-tick synthesis,
Lightweight ML Market Pattern recognition, persistent market data storage,
and autonomous multi-participant algorithmic trading.
"""

from dataclasses import dataclass
import random
import time
from typing import Dict, List, Optional, Tuple, Any

from chronos_match.market.data_loader import MarketDataLoader, StockCandle
from chronos_match.market.ml_impact_engine import MLMarketImpactEngine
from chronos_match.market.ml_pattern_model import LightMarketPatternModel, MarketRegime
from chronos_match.market.storage import MarketDataStore
from chronos_match.algo import AlgoTradingManager
from chronos_match.market.router import MultiBookRouter, SingleSymbolVenue, SymbolInfo
from chronos_match.protocol import to_fixed_price, from_fixed_price, OrderSide, OrderType, TradePacket


class HistoricalMarketReplayer:
    """
    Simulates authentic real-market conditions using real historical stock datasets,
    reactive ML price impact, online regime pattern classification, and persistent storage.
    """

    def __init__(
        self,
        router: MultiBookRouter,
        data_loader: Optional[MarketDataLoader] = None,
        data_store: Optional[MarketDataStore] = None,
        default_ticker: str = "NVDA",
    ):
        self.router = router
        self.data_loader = data_loader or MarketDataLoader()
        self.data_store = data_store or MarketDataStore()
        self.active_ticker = default_ticker.upper()

        self.impact_engines: Dict[str, MLMarketImpactEngine] = {}
        self.pattern_models: Dict[str, LightMarketPatternModel] = {}
        self.algo_managers: Dict[str, AlgoTradingManager] = {}
        self.candles_cache: Dict[str, List[StockCandle]] = {}
        self.current_candle_idx: Dict[str, int] = {}
        self.current_mid: Dict[str, float] = {}
        self.anchor_price: Dict[str, float] = {}
        self.macro_drift: Dict[str, float] = {}

        # Live candlestick synthesis per symbol
        self.live_candle: Dict[str, Dict[str, Any]] = {}
        self.candle_interval_sec: float = 5.0
        self.last_candle_time: Dict[str, float] = {}

        # Initialize default stock
        self.bind_stock(self.active_ticker)

    def bind_stock(self, ticker: str) -> SingleSymbolVenue:
        """
        Bind or dynamically register a real historical stock into the exchange router,
        ML pattern model, and persistence store.
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
            candles = []
            initial_p = 150.00
            daily_vol = 0.02
            avg_vol = 500_000
            tick_size = 0.01

        # Create or retrieve venue
        try:
            venue = self.router.get_venue(sym)
            if venue.info.initial_price and venue.info.initial_price > 0:
                initial_p = venue.info.initial_price
            else:
                venue.info.initial_price = initial_p
            if venue.last_trade_price is None:
                venue.last_trade_price = initial_p
        except KeyError:
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

        # Calibrate historical candles so the latest bar matches venue initial_p perfectly
        if candles and candles[-1].close > 0:
            scale = initial_p / candles[-1].close
            now_t = time.time()
            n = len(candles)
            calibrated_candles = []
            for i, c in enumerate(candles):
                t_str = time.strftime("%H:%M:%S", time.localtime(now_t - (n - i) * 15))
                calibrated_candles.append(
                    StockCandle(
                        date=t_str,
                        open=round(c.open * scale, 2),
                        high=round(c.high * scale, 2),
                        low=round(c.low * scale, 2),
                        close=round(c.close * scale, 2),
                        volume=c.volume,
                        adj_close=round(c.close * scale, 2),
                    )
                )
            candles = calibrated_candles

        self.candles_cache[sym] = candles
        self.candles_cache[venue.info.ticker] = candles
        self.current_candle_idx[sym] = len(candles) - 1 if candles else 0
        self.current_mid[sym] = initial_p
        self.current_mid[venue.info.ticker] = initial_p
        self.anchor_price[sym] = initial_p
        self.anchor_price[venue.info.ticker] = initial_p
        self.macro_drift[sym] = 0.0
        self.macro_drift[venue.info.ticker] = 0.0

        # Initialize dedicated ML Impact Engine
        if sym not in self.impact_engines:
            self.impact_engines[sym] = MLMarketImpactEngine(
                base_price=initial_p,
                daily_volatility=daily_vol,
                avg_daily_volume=avg_vol,
            )
        else:
            self.impact_engines[sym].set_baseline_price(initial_p)
        self.impact_engines[venue.info.ticker] = self.impact_engines[sym]

        # Initialize Lightweight ML Pattern Engine
        if sym not in self.pattern_models:
            self.pattern_models[sym] = LightMarketPatternModel(
                symbol=sym,
                base_price=initial_p,
                baseline_volatility=daily_vol,
            )
        self.pattern_models[venue.info.ticker] = self.pattern_models[sym]

        # Initialize Algorithmic Trading Suite
        if sym not in self.algo_managers:
            self.algo_managers[sym] = AlgoTradingManager(symbol=sym)
        self.algo_managers[venue.info.ticker] = self.algo_managers[sym]

        # Live candle tracking
        now = time.time()
        self.last_candle_time[sym] = now
        self.last_candle_time[venue.info.ticker] = now
        self.live_candle[sym] = {
            "date": time.strftime("%H:%M:%S"),
            "open": initial_p,
            "high": initial_p,
            "low": initial_p,
            "close": initial_p,
            "volume": 0,
            "trades": 0,
            "start_time": now,
        }
        self.live_candle[venue.info.ticker] = self.live_candle[sym]

        return venue

    def step_market_tick(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Advance one simulation step:
        1. Queries ML Pattern Model (Consolidation vs Momentum vs Volatility Chop).
        2. Computes effective price from macro historical anchor + ML order flow impact.
        3. Coordinates Market Makers, Momentum Bots, and realistic multi-participant noise.
        4. Updates live candlestick formation and persists data.
        """
        sym = (symbol or self.active_ticker).upper()
        venue = self.router.get_venue(sym)
        impact = self.impact_engines.get(sym)
        pattern_model = self.pattern_models.get(sym)
        algo_mgr = self.algo_managers.get(sym)

        if not impact or not pattern_model:
            return []

        # 1. Update Macro Anchor & Stochastic Microstructure Motion (Ornstein-Uhlenbeck)
        pred = pattern_model.predict()
        regime = pred.regime
        tick = venue.info.tick_size
        daily_vol = impact.daily_volatility
        anchor = self.anchor_price.get(sym, venue.info.initial_price or 100.0)

        # OU Process: smooth continuous oscillations within +/- 2.5% of anchor
        theta = 0.04
        sigma = max(0.0004, min(0.0022, daily_vol * 0.05))
        m_drift = self.macro_drift.get(sym, 0.0)
        ml_nudge = pred.predicted_drift * 0.10
        m_drift += -theta * m_drift + sigma * random.gauss(0, 1) + ml_nudge
        m_drift = max(-0.025, min(0.025, m_drift))
        self.macro_drift[sym] = m_drift
        self.macro_drift[venue.info.ticker] = m_drift

        # Micro-tick jitter
        micro = random.gauss(0, 1) * 0.0002
        new_mid = max(tick * 5, round(anchor * (1.0 + m_drift + micro), 2))
        self.current_mid[sym] = new_mid
        self.current_mid[venue.info.ticker] = new_mid
        impact.set_baseline_price(new_mid)
        effective_mid = new_mid
        events: List[Dict[str, Any]] = []

        # 2. Market Maker Quoting
        if algo_mgr and algo_mgr.market_maker.active:
            res_p, bid_p, ask_p = algo_mgr.market_maker.compute_quotes(
                mid_price=effective_mid,
                volatility=impact.daily_volatility * pred.spread_multiplier,
                impact_engine=impact,
            )
            algo_mgr.market_maker.total_quotes_placed += 2

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
                    self._on_trade_matched(venue, sym, int(side_enum), trade_p, m["quantity"], impact, pattern_model, algo_mgr, m)
                    events.append({"type": "MOMENTUM_TRADE", "side": action, "price": trade_p, "qty": m["quantity"]})

        # 4. Multi-Participant Natural Market Flow & Dynamic Book Depth
        spread_mult = max(1.0, impact.get_spread_multiplier() * pred.spread_multiplier)
        half_spread = max(tick, round(effective_mid * 0.0003 * spread_mult, 2))
        best_bid_p = from_fixed_price(venue.book.best_bid_price)
        best_ask_p = from_fixed_price(venue.book.best_ask_price)

        target_bid = round(effective_mid - half_spread, 2)
        target_ask = round(effective_mid + half_spread, 2)

        # Replenish top-of-book liquidity levels if disconnected or exhausted
        if best_bid_p <= 0 or abs(best_bid_p - target_bid) > half_spread * 2.5:
            for lvl in range(3):
                p_lvl = round(target_bid - lvl * tick * 2, 2)
                b_oid = self.router.allocate_order_id()
                self.router.client_order_registry[b_oid] = 99
                venue.book.add_limit_order(
                    order_id=b_oid,
                    client_id=99,
                    price=to_fixed_price(p_lvl),
                    quantity=random.choice([200, 500, 1000]),
                    side=int(OrderSide.BUY),
                    symbol_id=venue.info.symbol_id,
                )
                venue.total_orders += 1
            best_bid_p = target_bid

        if best_ask_p <= 0 or abs(best_ask_p - target_ask) > half_spread * 2.5:
            for lvl in range(3):
                p_lvl = round(target_ask + lvl * tick * 2, 2)
                a_oid = self.router.allocate_order_id()
                self.router.client_order_registry[a_oid] = 99
                venue.book.add_limit_order(
                    order_id=a_oid,
                    client_id=99,
                    price=to_fixed_price(p_lvl),
                    quantity=random.choice([200, 500, 1000]),
                    side=int(OrderSide.SELL),
                    symbol_id=venue.info.symbol_id,
                )
                venue.total_orders += 1
            best_ask_p = target_ask

        # Regulate trading aggression based on market state
        if regime == MarketRegime.MOMENTUM_BREAKOUT_BULL:
            buy_bias = 0.75
            trade_prob = 0.92
        elif regime == MarketRegime.MOMENTUM_BREAKOUT_BEAR:
            buy_bias = 0.25
            trade_prob = 0.92
        elif regime == MarketRegime.VOLATILITY_EXPANSION_CHOP:
            buy_bias = 0.50
            trade_prob = 0.95
        else:  # BALANCED_CONSOLIDATION
            buy_bias = 0.50 + 0.30 * min(1.0, max(-1.0, pred.predicted_drift / 0.005))
            trade_prob = 0.88

        if random.random() < trade_prob:
            side = OrderSide.BUY if random.random() < buy_bias else OrderSide.SELL

            # 70% of orders are aggressive spread-crossing orders (market sweeps)
            is_market_sweep = random.random() < 0.70
            if is_market_sweep:
                order_p = best_ask_p if side == OrderSide.BUY else best_bid_p
                # Occasional sweep into deeper level
                if random.random() < 0.25:
                    order_p = round(order_p + (tick if side == OrderSide.BUY else -tick), 2)
            else:
                offset = random.choice([1, 2, 3]) * tick * spread_mult
                order_p = round((best_bid_p - offset) if side == OrderSide.BUY else (best_ask_p + offset), 2)

            qty = random.choice([100, 200, 300, 500, 1000 if regime == MarketRegime.VOLATILITY_EXPANSION_CHOP else 150])

            oid = self.router.allocate_order_id()
            self.router.client_order_registry[oid] = 99  # External market participant

            matches = venue.book.add_limit_order(
                order_id=oid,
                client_id=99,
                price=to_fixed_price(round(order_p, 2)),
                quantity=qty,
                side=int(side),
                symbol_id=venue.info.symbol_id,
            )
            venue.total_orders += 1

            for m in matches:
                trade_p = from_fixed_price(m["execution_price"])
                self._on_trade_matched(venue, sym, int(side), trade_p, m["quantity"], impact, pattern_model, algo_mgr, m)
                events.append({"type": "NATURAL_FILL", "price": trade_p, "qty": m["quantity"]})

        # 5. Check Candlestick Rollover Interval
        self._check_candle_rollover(sym, daily_vol, tick)

        # Periodic ML regime logging & order flow decay
        pattern_model.step_decay(elapsed_sec=0.05)
        if random.random() < 0.05:
            self.data_store.record_ml_regime(
                symbol=sym,
                timestamp_ns=time.perf_counter_ns(),
                regime=pred.regime.value,
                confidence=pred.confidence,
                predicted_drift=pred.predicted_drift,
                volatility=pred.expected_volatility,
                spread_multiplier=pred.spread_multiplier,
            )

        return events

    def _on_trade_matched(
        self,
        venue: SingleSymbolVenue,
        sym: str,
        side: int,
        trade_p: float,
        qty: int,
        impact: MLMarketImpactEngine,
        pattern_model: LightMarketPatternModel,
        algo_mgr: Optional[AlgoTradingManager],
        match_info: Dict[str, Any],
    ) -> None:
        """Internal callback when any trade match executes."""
        venue.last_trade_price = trade_p
        venue.total_trades += 1
        venue.total_volume += qty
        venue.high_price = max(venue.high_price, trade_p)
        venue.low_price = min(venue.low_price, trade_p)

        # 1. Record Trade & Inspect Whale Flow
        trade_id = match_info.get("trade_id", venue.total_trades)
        trade_data = {
            "trade_id": trade_id,
            "symbol": venue.info.ticker,
            "side": "BUY" if side == int(OrderSide.BUY) else "SELL",
            "price": trade_p,
            "quantity": qty,
            "latency_us": 0.12,
            "time": time.strftime("%H:%M:%S"),
        }
        venue.recent_trades.insert(0, trade_data)
        if len(venue.recent_trades) > 40:
            venue.recent_trades.pop()

        venue.price_history.append({"price": trade_p, "time": trade_data["time"]})
        if len(venue.price_history) > 60:
            venue.price_history.pop(0)

        tp = TradePacket(
            trade_id=trade_id,
            buy_order_id=match_info.get("buy_order_id", 0),
            sell_order_id=match_info.get("sell_order_id", 0),
            price=to_fixed_price(trade_p),
            quantity=qty,
            symbol_id=venue.info.symbol_id,
            aggressor_side=side,
            match_timestamp_ns=time.perf_counter_ns(),
            latency_ns=120,
        )
        venue.whale_detector.inspect_trade(tp)

        # 2. Update ML Models
        impact.record_execution(side, qty, trade_p)
        pattern_model.record_trade(trade_p, qty, side)
        pattern_model.update_order_book_depth(
            from_fixed_price(venue.book.best_bid_price),
            from_fixed_price(venue.book.best_ask_price),
            venue.book.best_bid_volume,
            venue.book.best_ask_volume,
        )

        # 3. Inform Algo Bot
        if algo_mgr:
            algo_mgr.on_trade_executed(match_info.get("buy_order_id", 0), int(OrderSide.BUY), trade_p, qty)
            algo_mgr.on_trade_executed(match_info.get("sell_order_id", 0), int(OrderSide.SELL), trade_p, qty)

        # 4. Persist Tick
        best_bid = from_fixed_price(venue.book.best_bid_price)
        best_ask = from_fixed_price(venue.book.best_ask_price)
        self.data_store.record_tick(
            symbol=sym,
            timestamp_ns=time.perf_counter_ns(),
            price=trade_p,
            quantity=qty,
            side=side,
            best_bid=best_bid,
            best_ask=best_ask,
            ofi=impact.ofi,
            regime=pattern_model.current_regime.value,
        )

        # 4. Synthesize Live Candlestick
        self._update_live_candle(sym, trade_p, qty)

    def _update_live_candle(self, sym: str, price: float, volume: int) -> None:
        """Accumulate live tick into active candle bar."""
        now = time.time()
        bar = self.live_candle.get(sym)
        if not bar:
            bar = {
                "date": time.strftime("%H:%M:%S"),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0,
                "trades": 0,
                "start_time": now,
            }
            self.live_candle[sym] = bar

        # Update high/low/close/volume
        bar["high"] = max(bar["high"], price)
        bar["low"] = min(bar["low"], price)
        bar["close"] = price
        bar["volume"] += volume
        bar["trades"] += 1

    def _check_candle_rollover(self, sym: str, daily_vol: float = 0.02, tick: float = 0.01) -> None:
        """Check if candle interval elapsed, ensure organic shape, persist and roll over."""
        now = time.time()
        last_t = self.last_candle_time.get(sym, now)
        if (now - last_t) >= self.candle_interval_sec:
            bar = self.live_candle.get(sym)
            if bar:
                # Ensure every candle has authentic body and wicks (never a 0-pixel flat line)
                if bar["high"] <= bar["low"] + 0.001:
                    w_span = max(tick * 4, round(bar["close"] * daily_vol * 0.003, 2))
                    bar["high"] = round(max(bar["open"], bar["close"]) + w_span * 0.6, 2)
                    bar["low"] = round(max(0.01, min(bar["open"], bar["close"]) - w_span * 0.4), 2)
                    bar["volume"] = max(bar["volume"], random.randint(500, 2500))

                # Store completed candle in SQLite
                self.data_store.record_candle(
                    symbol=sym,
                    timestamp=int(now),
                    date_str=bar["date"],
                    open_p=bar["open"],
                    high_p=bar["high"],
                    low_p=bar["low"],
                    close_p=bar["close"],
                    volume=bar["volume"],
                    trades_count=max(1, bar["trades"]),
                )
                # Push into cached candles so chart displays it
                self.candles_cache.setdefault(sym, []).append(
                    StockCandle(
                        date=bar["date"],
                        open=bar["open"],
                        high=bar["high"],
                        low=bar["low"],
                        close=bar["close"],
                        volume=bar["volume"],
                    )
                )
                if len(self.candles_cache[sym]) > 120:
                    self.candles_cache[sym] = self.candles_cache[sym][-120:]

                # Start fresh live candle with open = previous close (smooth continuity)
                prev_close = bar["close"]
                self.last_candle_time[sym] = now
                self.live_candle[sym] = {
                    "date": time.strftime("%H:%M:%S"),
                    "open": prev_close,
                    "high": prev_close,
                    "low": prev_close,
                    "close": prev_close,
                    "volume": 0,
                    "trades": 0,
                    "start_time": now,
                }

    def get_candlesticks(self, symbol: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Return historical candlestick list for active or specified symbol, including live candle."""
        sym = (symbol or self.active_ticker).upper()
        candles = self.candles_cache.get(sym, [])
        if not candles:
            try:
                candles = self.data_loader.load_candles(sym, max_bars=limit)
                self.candles_cache[sym] = candles
            except Exception:
                candles = []

        bars = [
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

        # Append current building live candle if active
        live = self.live_candle.get(sym)
        if live and live["trades"] > 0:
            bars.append({
                "date": live["date"],
                "open": live["open"],
                "high": live["high"],
                "low": live["low"],
                "close": live["close"],
                "volume": live["volume"],
            })

        return bars[-limit:] if limit > 0 else bars
