"""
ChronosMatch: Real-Market Replay & Adaptive Simulation Engine.

Orchestrates real-world historical market data streaming, micro-tick synthesis,
Lightweight ML Market Pattern recognition, persistent market data storage,
and autonomous multi-participant algorithmic trading.
"""

from collections import deque
from dataclasses import dataclass
import random
import time
from typing import Dict, List, Optional, Tuple, Any

from chronos_match.market.data_loader import MarketDataLoader, StockCandle
from chronos_match.market.ml_impact_engine import MLMarketImpactEngine
from chronos_match.market.ml_pattern_model import LightMarketPatternModel, MarketRegime
from chronos_match.market.candlestick_patterns import CandlestickPatternRecognizer, DetectedPattern
from chronos_match.market.storage import MarketDataStore
from chronos_match.algo import AlgoTradingManager
from chronos_match.market.router import MultiBookRouter, SingleSymbolVenue, SymbolInfo
from chronos_match.protocol import to_fixed_price, from_fixed_price, OrderSide, OrderType, TradePacket


@dataclass
class ReplayerWaveState:
    """Dynamic multi-bar wave momentum state machine for authentic market cycles."""
    phase: str  # "BULL_IMPULSE", "BULL_PULLBACK", "RANGE_CONSOLIDATION", "BEAR_IMPULSE", "BEAR_PULLBACK", "REVERSAL_BOUNCE"
    bars_remaining: int
    swing_high: float
    swing_low: float
    atr: float
    cycle_count: int = 0


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
        self.wave_states: Dict[str, ReplayerWaveState] = {}
        self.candles_cache: Dict[str, List[StockCandle]] = {}
        self.current_candle_idx: Dict[str, int] = {}
        self.current_mid: Dict[str, float] = {}
        self.anchor_price: Dict[str, float] = {}
        self.macro_drift: Dict[str, float] = {}
        self.latest_pattern: Dict[str, DetectedPattern] = {}

        # Live candlestick synthesis per symbol
        self.live_candle: Dict[str, Dict[str, Any]] = {}
        self.candle_interval_sec: float = 5.0
        self.last_candle_time: Dict[str, float] = {}
        self.sim_orders_queue: Dict[str, deque] = {}

        # Initialize default stock
        self.bind_stock(self.active_ticker)

    def _track_sim_order(self, venue: SingleSymbolVenue, symbol: str, order_id: int):
        """Track simulated background orders and cancel oldest to prevent stale order build-up."""
        q = self.sim_orders_queue.setdefault(symbol, deque())
        q.append(order_id)
        while len(q) > 300:
            old_id = q.popleft()
            venue.book.cancel_order(old_id)

    def bind_stock(self, ticker: str) -> SingleSymbolVenue:
        """
        Bind or dynamically register a real historical stock into the exchange router,
        ML pattern model, and persistence store.
        """
        sym = ticker.strip().upper()
        self.active_ticker = sym

        # 1. Determine benchmark venue price
        try:
            existing_venue = self.router.get_venue(sym)
            target_p = existing_venue.info.initial_price
        except KeyError:
            target_p = None

        if target_p and target_p > 0:
            initial_p = target_p
            tick_size = 0.50 if "BTC" in sym else (0.10 if "ETH" in sym else 0.01)
            daily_vol = 0.025 if any(c in sym for c in ("BTC", "ETH", "SOL")) else 0.018
            avg_vol = 1_000_000
        else:
            try:
                profile = self.data_loader.compute_profile(sym, lookback=60)
                initial_p = profile["latest_price"]
                daily_vol = profile["volatility_daily"]
                avg_vol = profile["avg_volume"]
                tick_size = profile["tick_size"]
            except Exception:
                initial_p = 65000.0 if "BTC" in sym else (3500.0 if "ETH" in sym else (150.0 if "SOL" in sym else 130.0))
                daily_vol = 0.02
                avg_vol = 500_000
                tick_size = 0.01

        # 2. Check if valid session candles are already persisted in SQLite
        stored_raw = self.data_store.get_recent_candles(sym, limit=180)
        if not stored_raw and "-" in sym:
            stored_raw = self.data_store.get_recent_candles(sym.split("-")[0], limit=180)

        candles: List[StockCandle] = []
        valid_stored = False
        if stored_raw and len(stored_raw) >= 10:
            last_p = stored_raw[-1]["close"]
            # Valid if stored close matches expected benchmark price within 2.5%
            if initial_p and abs(last_p - initial_p) / initial_p <= 0.025:
                valid_stored = True
                for r in stored_raw:
                    candles.append(
                        StockCandle(
                            date=r["date_str"],
                            open=round(r["open"], 2),
                            high=round(r["high"], 2),
                            low=round(r["low"], 2),
                            close=round(r["close"], 2),
                            volume=int(r["volume"]),
                            adj_close=round(r["close"], 2),
                        )
                    )
                initial_p = candles[-1].close

        if not valid_stored:
            # Load from historical data loader
            try:
                candles = self.data_loader.load_candles(sym, max_bars=180)
            except Exception:
                candles = []

            # Synthesize authentic candle proportions with realistic ATR
            if candles and candles[-1].close > 0:
                scale = initial_p / candles[-1].close
                now_t = time.time()
                n = len(candles)
                atr = max(tick_size * 4, initial_p * 0.003)
                calibrated_candles = []
                for i, c in enumerate(candles):
                    t_str = time.strftime("%H:%M:%S", time.localtime(now_t - (n - i) * 60))
                    bar_o = round(c.open * scale, 2)
                    bar_c = round(c.close * scale, 2)
                    bar_h = round(c.high * scale, 2)
                    bar_l = round(c.low * scale, 2)

                    # Ensure realistic body and distinct shadows
                    min_tr = atr * 0.4
                    if (bar_h - bar_l) < min_tr:
                        body_top = max(bar_o, bar_c)
                        body_bot = min(bar_o, bar_c)
                        bar_h = round(body_top + atr * 0.25, 2)
                        bar_l = round(max(tick_size, body_bot - atr * 0.25), 2)

                    calibrated_candles.append(
                        StockCandle(
                            date=t_str,
                            open=bar_o,
                            high=bar_h,
                            low=bar_l,
                            close=bar_c,
                            volume=c.volume,
                            adj_close=bar_c,
                        )
                    )
                candles = calibrated_candles

                # Clear old corrupted candles and persist clean calibrated session
                try:
                    with self.data_store._lock:
                        cur = self.data_store.conn.cursor()
                        cur.execute("DELETE FROM market_candles WHERE symbol = ?", (sym.upper(),))
                        if "-" in sym:
                            cur.execute("DELETE FROM market_candles WHERE symbol = ?", (sym.split("-")[0].upper(),))
                        self.data_store.conn.commit()
                except Exception:
                    pass

                for sc in candles:
                    self.data_store.record_candle(
                        symbol=sym,
                        timestamp=int(now_t - (n - candles.index(sc)) * 60),
                        date_str=sc.date,
                        open_p=sc.open,
                        high_p=sc.high,
                        low_p=sc.low,
                        close_p=sc.close,
                        volume=sc.volume,
                        trades_count=max(1, sc.volume // 50),
                    )

        # Create or retrieve venue and reseed liquidity centered around authentic initial_p
        try:
            venue = self.router.get_venue(sym)
            self.router.reseed_symbol(sym, initial_p)
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

        # Initialize Institutional Wave Dynamics State Machine
        if initial_p > 10000:
            sym_atr = max(tick_size * 20, initial_p * 0.005)
        elif initial_p > 1000:
            sym_atr = max(tick_size * 20, initial_p * 0.006)
        elif initial_p > 100:
            sym_atr = max(tick_size * 20, initial_p * 0.007)
        else:
            sym_atr = max(tick_size * 10, initial_p * 0.012)

        self.wave_states[sym] = ReplayerWaveState(
            phase="BULL_IMPULSE",
            bars_remaining=random.randint(3, 5),
            swing_high=round(initial_p + sym_atr * 2.8, 2),
            swing_low=round(initial_p - sym_atr * 2.8, 2),
            atr=sym_atr,
        )
        self.wave_states[venue.info.ticker] = self.wave_states[sym]

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

        # 1. Continuous Walk-Forward Stochastic Microstructure with ML Regime Guidance & Wave Dynamics
        pred = pattern_model.predict()
        regime = pred.regime
        tick = venue.info.tick_size
        daily_vol = max(0.008, impact.daily_volatility)
        cur_mid = self.current_mid.get(sym, venue.info.initial_price or 100.0)

        # Retrieve or initialize wave state
        w_state = self.wave_states.get(sym)
        if not w_state:
            sym_atr = max(tick * 15, cur_mid * 0.006)
            w_state = ReplayerWaveState(
                phase="BULL_IMPULSE",
                bars_remaining=random.randint(3, 5),
                swing_high=cur_mid + sym_atr * 2.8,
                swing_low=cur_mid - sym_atr * 2.8,
                atr=sym_atr,
            )
            self.wave_states[sym] = w_state

        # Dynamic drift guided by ML regime prediction, Stoikov micro-price, and Institutional Wave Momentum
        micro_bias = (pattern_model.micro_price_delta / max(1.0, cur_mid)) * 0.25

        if w_state.phase == "BULL_IMPULSE":
            drift = max(0.00015, (w_state.atr * 0.08 / cur_mid)) + pred.predicted_drift * 0.8 + micro_bias
            vol_mult = 1.3
            if cur_mid >= w_state.swing_high:
                drift = -0.00025
        elif w_state.phase == "BULL_PULLBACK":
            drift = -max(0.00012, (w_state.atr * 0.05 / cur_mid)) + micro_bias * 0.5
            vol_mult = 0.85
            if cur_mid <= w_state.swing_low:
                drift = 0.00025
        elif w_state.phase == "BEAR_IMPULSE":
            drift = -max(0.00018, (w_state.atr * 0.09 / cur_mid)) + pred.predicted_drift * 0.8 + micro_bias
            vol_mult = 1.4
            if cur_mid <= w_state.swing_low:
                drift = 0.00025
        elif w_state.phase == "BEAR_PULLBACK":
            drift = max(0.00012, (w_state.atr * 0.05 / cur_mid)) + micro_bias * 0.5
            vol_mult = 0.85
        elif w_state.phase == "REVERSAL_BOUNCE":
            drift = max(0.00025, (w_state.atr * 0.12 / cur_mid)) + micro_bias
            vol_mult = 1.5
        elif regime == MarketRegime.VOLATILITY_EXPANSION_CHOP:
            drift = 0.0004 if random.random() < 0.5 else -0.0004
            vol_mult = 2.0
        else:  # RANGE_CONSOLIDATION / BALANCED
            range_mid = (w_state.swing_high + w_state.swing_low) / 2.0
            deviation = (cur_mid - range_mid) / max(1.0, w_state.atr)
            drift = -0.00030 * deviation + pred.predicted_drift * 0.3 + micro_bias * 0.5
            vol_mult = 1.0

        # Authentic tick volatility shock calibrated for high-frequency (25 ticks/sec) loop
        drift = drift * 0.05
        step_sigma = (daily_vol * 0.001) * vol_mult
        shock = random.gauss(0, 1) * max(0.00005, step_sigma)
        ofi_push = (impact.ofi / max(1000.0, impact.avg_daily_volume * 0.005)) * 0.00002

        new_mid = round(cur_mid * (1.0 + drift + shock + ofi_push), 2)
        min_p = max(tick * 5, round((venue.info.initial_price or 100.0) * 0.90, 2))
        max_p = round((venue.info.initial_price or 100.0) * 1.10, 2)
        new_mid = max(min_p, min(max_p, new_mid))

        self.current_mid[sym] = new_mid
        self.current_mid[venue.info.ticker] = new_mid
        impact.set_baseline_price(new_mid)
        effective_mid = new_mid
        events: List[Dict[str, Any]] = []

        # 2. Market Maker Quoting
        if algo_mgr and algo_mgr.market_maker.active:
            # Cancel prior resting MM quotes to prevent stale quote build-up
            if algo_mgr.market_maker.active_bid_id:
                venue.book.cancel_order(algo_mgr.market_maker.active_bid_id)
                algo_mgr.market_maker.active_bid_id = None
            if algo_mgr.market_maker.active_ask_id:
                venue.book.cancel_order(algo_mgr.market_maker.active_ask_id)
                algo_mgr.market_maker.active_ask_id = None

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
            algo_mgr.market_maker.active_bid_id = bid_oid

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
            algo_mgr.market_maker.active_ask_id = ask_oid

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

        # 3.5. Reinforcement Learning Optimal Execution Agent (DQN)
        if algo_mgr and algo_mgr.rl_execution_agent.active and algo_mgr.rl_execution_agent.active_parent_order:
            best_b_val = from_fixed_price(venue.book.best_bid_price)
            best_a_val = from_fixed_price(venue.book.best_ask_price)
            eff_bid = best_b_val if best_b_val > 0 else round(effective_mid - tick, 2)
            eff_ask = best_a_val if best_a_val > 0 else round(effective_mid + tick, 2)
            spread_mult_val = max(1.0, impact.get_spread_multiplier() * pred.spread_multiplier)

            latest_pat = self.latest_pattern.get(sym)
            pat_sentiment = (
                latest_pat.sentiment.value if latest_pat
                else ("BULLISH" if "BULL" in regime.value else ("BEARISH" if "BEAR" in regime.value else "NEUTRAL"))
            )
            rl_state = algo_mgr.rl_execution_agent.extract_microstructure_state(
                market_mid=effective_mid,
                best_bid=eff_bid,
                best_ask=eff_ask,
                bid_vol=venue.book.best_bid_volume or 100,
                ask_vol=venue.book.best_ask_volume or 100,
                ofi=impact.ofi,
                daily_vol=daily_vol,
                spread_mult=spread_mult_val,
                predicted_drift=pred.predicted_drift,
                regime_value=regime.value,
                pattern_sentiment=pat_sentiment,
                tick_size=tick,
            )
            child_order = algo_mgr.rl_execution_agent.step_execution_policy(
                state=rl_state,
                best_bid=eff_bid,
                best_ask=eff_ask,
                tick_size=tick,
            )
            if child_order:
                child_oid = self.router.allocate_order_id()
                self.router.client_order_registry[child_oid] = algo_mgr.rl_execution_agent.bot_id
                matches = venue.book.add_limit_order(
                    order_id=child_oid,
                    client_id=algo_mgr.rl_execution_agent.bot_id,
                    price=to_fixed_price(child_order["price"]),
                    quantity=child_order["quantity"],
                    side=int(child_order["side"]),
                    symbol_id=venue.info.symbol_id,
                )
                venue.total_orders += 1
                for m in matches:
                    trade_p = from_fixed_price(m["execution_price"])
                    self._on_trade_matched(venue, sym, int(child_order["side"]), trade_p, m["quantity"], impact, pattern_model, algo_mgr, m)
                    events.append({
                        "type": "RL_EXECUTION_FILL",
                        "action": child_order["action"],
                        "side": "BUY" if child_order["side"] == int(OrderSide.BUY) else "SELL",
                        "price": trade_p,
                        "qty": m["quantity"],
                        "maker": child_order["is_maker"],
                    })

        # 4. Multi-Participant Natural Market Flow & Dynamic Book Depth
        spread_mult = max(1.0, impact.get_spread_multiplier() * pred.spread_multiplier)
        half_spread = max(tick, round(effective_mid * 0.0003 * spread_mult, 2))
        best_bid_p = from_fixed_price(venue.book.best_bid_price)
        best_ask_p = from_fixed_price(venue.book.best_ask_price)

        target_bid = round(effective_mid - half_spread, 2)
        target_ask = round(effective_mid + half_spread, 2)

        # Cull stale background simulated orders that would cross or distort the spread
        venue.book.cull_stale(
            max_bid=to_fixed_price(target_bid),
            min_ask=to_fixed_price(target_ask),
        )
        best_bid_p = from_fixed_price(venue.book.best_bid_price)
        best_ask_p = from_fixed_price(venue.book.best_ask_price)

        # Replenish top-of-book liquidity levels if disconnected, exhausted, or thin
        l2_snap = venue.book.get_l2_snapshot(8)
        num_bids = len(l2_snap.get("bids", []))
        num_asks = len(l2_snap.get("asks", []))

        if best_bid_p <= 0 or abs(best_bid_p - target_bid) > half_spread * 2.5 or num_bids < 6:
            for lvl in range(8):
                p_lvl = round(target_bid - lvl * tick * 2, 2)
                if p_lvl <= 0:
                    break
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
                self._track_sim_order(venue, sym, b_oid)
                venue.total_orders += 1
            best_bid_p = from_fixed_price(venue.book.best_bid_price) or target_bid

        if best_ask_p <= 0 or abs(best_ask_p - target_ask) > half_spread * 2.5 or num_asks < 6:
            for lvl in range(8):
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
                self._track_sim_order(venue, sym, a_oid)
                venue.total_orders += 1
            best_ask_p = from_fixed_price(venue.book.best_ask_price) or target_ask

        # Regulate trading aggression based on market state and active wave phase
        if w_state and w_state.phase in ("BULL_IMPULSE", "REVERSAL_BOUNCE"):
            buy_bias = 0.70
            trade_prob = 0.92
        elif w_state and w_state.phase in ("BEAR_IMPULSE", "BULL_PULLBACK"):
            buy_bias = 0.30
            trade_prob = 0.92
        elif w_state and w_state.phase == "BEAR_PULLBACK":
            buy_bias = 0.65
            trade_prob = 0.88
        elif regime == MarketRegime.VOLATILITY_EXPANSION_CHOP:
            buy_bias = 0.50
            trade_prob = 0.95
        elif regime == MarketRegime.MOMENTUM_BREAKOUT_BULL:
            buy_bias = 0.72
            trade_prob = 0.92
        elif regime == MarketRegime.MOMENTUM_BREAKOUT_BEAR:
            buy_bias = 0.28
            trade_prob = 0.92
        else:  # RANGE_CONSOLIDATION / BALANCED
            range_mid = (w_state.swing_high + w_state.swing_low) / 2.0 if w_state else effective_mid
            dist = (effective_mid - range_mid) / max(0.1, w_state.atr if w_state else 1.0)
            buy_bias = max(0.20, min(0.80, 0.50 - 0.25 * dist))
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

            if order_p <= 0:
                order_p = target_bid if side == OrderSide.BUY else target_ask

            qty = random.choice([100, 200, 300, 500, 1000 if regime == MarketRegime.VOLATILITY_EXPANSION_CHOP else 150])

            oid = self.router.allocate_order_id()
            self.router.client_order_registry[oid] = 99  # External market participant
            self._track_sim_order(venue, sym, oid)

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
            b_oid = match_info.get("buy_order_id", 0)
            s_oid = match_info.get("sell_order_id", 0)
            b_cid = self.router.client_order_registry.get(b_oid, b_oid)
            s_cid = self.router.client_order_registry.get(s_oid, s_oid)
            aggressor = match_info.get("aggressor_side", 1)
            b_is_maker = (aggressor == 2)
            s_is_maker = (aggressor == 1)
            algo_mgr.on_trade_executed(b_cid, int(OrderSide.BUY), trade_p, qty, is_maker=b_is_maker)
            algo_mgr.on_trade_executed(s_cid, int(OrderSide.SELL), trade_p, qty, is_maker=s_is_maker)

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
        """Check if candle interval elapsed, recognize patterns, persist to DB and feature store, and roll over."""
        now = time.time()
        last_t = self.last_candle_time.get(sym, now)
        if (now - last_t) >= self.candle_interval_sec:
            bar = self.live_candle.get(sym)
            if bar:
                pattern_model = self.pattern_models.get(sym)
                pred = pattern_model.predict() if pattern_model else None
                regime_bias = pred.predicted_drift if pred else 0.0

                w_state = self.wave_states.get(sym)
                if not w_state:
                    cur_p = bar["close"]
                    sym_atr = max(tick * 15, cur_p * 0.006)
                    w_state = ReplayerWaveState(
                        phase="BULL_IMPULSE",
                        bars_remaining=random.randint(3, 5),
                        swing_high=cur_p + sym_atr * 2.8,
                        swing_low=cur_p - sym_atr * 2.8,
                        atr=sym_atr,
                    )
                    self.wave_states[sym] = w_state

                # Check ML model regime and latest pattern context to drive next wave phase
                latest_pat = self.latest_pattern.get(sym)
                regime_enum = pred.regime if pred else MarketRegime.BALANCED_CONSOLIDATION
                rsi_val = pattern_model.rsi if pattern_model else 50.0

                w_state.bars_remaining -= 1
                if w_state.bars_remaining <= 0:
                    w_state.cycle_count += 1
                    if w_state.phase == "BULL_IMPULSE":
                        # If RSI is high (>65) or wave duration expired, enter shallow pullback (stair-step rally)
                        w_state.phase = "BULL_PULLBACK" if rsi_val > 62 else "RANGE_CONSOLIDATION"
                        w_state.bars_remaining = random.randint(2, 4)
                        w_state.swing_high = max(w_state.swing_high, bar["high"])
                        w_state.swing_low = max(tick * 5, bar["close"] - w_state.atr * random.uniform(0.9, 1.4))
                    elif w_state.phase == "BULL_PULLBACK":
                        # Retracement exhausted: if bullish pattern formed or regime bull -> resume rally!
                        if regime_enum == MarketRegime.MOMENTUM_BREAKOUT_BULL or (latest_pat and latest_pat.sentiment == "BULLISH"):
                            w_state.phase = "BULL_IMPULSE"
                            w_state.bars_remaining = random.randint(3, 6)
                            w_state.swing_high = bar["close"] + w_state.atr * random.uniform(2.2, 3.2)
                        elif random.random() < 0.40:
                            w_state.phase = "REVERSAL_BOUNCE"
                            w_state.bars_remaining = random.randint(1, 2)
                            w_state.swing_high = bar["close"] + w_state.atr * random.uniform(1.8, 2.5)
                        else:
                            w_state.phase = "RANGE_CONSOLIDATION"
                            w_state.bars_remaining = random.randint(3, 5)
                    elif w_state.phase == "RANGE_CONSOLIDATION":
                        # Breakout dynamically guided by ML regime prediction
                        if regime_enum == MarketRegime.MOMENTUM_BREAKOUT_BULL or (latest_pat and latest_pat.sentiment == "BULLISH"):
                            w_state.phase = "BULL_IMPULSE"
                            w_state.bars_remaining = random.randint(3, 6)
                            w_state.swing_high = bar["close"] + w_state.atr * random.uniform(2.2, 3.2)
                        elif regime_enum == MarketRegime.MOMENTUM_BREAKOUT_BEAR or (latest_pat and latest_pat.sentiment == "BEARISH"):
                            w_state.phase = "BEAR_IMPULSE"
                            w_state.bars_remaining = random.randint(3, 5)
                            w_state.swing_low = max(tick * 5, bar["close"] - w_state.atr * random.uniform(2.2, 3.2))
                        elif rsi_val > 54:
                            w_state.phase = "BULL_IMPULSE"
                            w_state.bars_remaining = random.randint(3, 5)
                            w_state.swing_high = bar["close"] + w_state.atr * random.uniform(2.0, 3.0)
                        else:
                            w_state.phase = "BEAR_IMPULSE"
                            w_state.bars_remaining = random.randint(3, 5)
                            w_state.swing_low = max(tick * 5, bar["close"] - w_state.atr * random.uniform(2.0, 3.0))
                    elif w_state.phase == "BEAR_IMPULSE":
                        w_state.phase = "BEAR_PULLBACK" if rsi_val < 38 else "RANGE_CONSOLIDATION"
                        w_state.bars_remaining = random.randint(2, 4)
                        w_state.swing_low = min(w_state.swing_low, bar["low"])
                        w_state.swing_high = bar["close"] + w_state.atr * random.uniform(0.9, 1.4)
                    elif w_state.phase == "BEAR_PULLBACK":
                        if regime_enum == MarketRegime.MOMENTUM_BREAKOUT_BEAR or (latest_pat and latest_pat.sentiment == "BEARISH"):
                            w_state.phase = "BEAR_IMPULSE"
                            w_state.bars_remaining = random.randint(3, 5)
                            w_state.swing_low = max(tick * 5, bar["close"] - w_state.atr * random.uniform(2.0, 3.0))
                        else:
                            w_state.phase = "RANGE_CONSOLIDATION"
                            w_state.bars_remaining = random.randint(3, 5)
                    else:  # REVERSAL_BOUNCE
                        w_state.phase = "BULL_IMPULSE"
                        w_state.bars_remaining = random.randint(4, 7)
                        w_state.swing_high = bar["close"] + w_state.atr * random.uniform(2.2, 3.2)

                atr = w_state.atr

                # Ensure every candle has authentic natural body and distinct wicks
                if bar["trades"] == 0 or abs(bar["close"] - bar["open"]) < tick:
                    # Synthesize authentic candle geometry driven by active wave phase
                    if w_state.phase == "BULL_IMPULSE":
                        is_green = random.random() < 0.80
                        move = atr * random.uniform(0.5, 1.2) if is_green else atr * random.uniform(0.15, 0.4)
                        bar["close"] = round(bar["open"] + (move if is_green else -move), 2)
                        wick_u = round(atr * random.uniform(0.10, 0.35), 2)
                        wick_l = round(atr * random.uniform(0.10, 0.30), 2)
                        bar["volume"] = random.randint(15000, 45000)
                    elif w_state.phase == "BULL_PULLBACK":
                        # Retracement candle (red!)
                        is_red = random.random() < 0.78
                        move = atr * random.uniform(0.4, 0.8) if is_red else atr * random.uniform(0.1, 0.3)
                        bar["close"] = round(bar["open"] - (move if is_red else -move), 2)
                        wick_u = round(atr * random.uniform(0.20, 0.45), 2)
                        wick_l = round(atr * random.uniform(0.10, 0.25), 2)
                        bar["volume"] = random.randint(5000, 18000)
                    elif w_state.phase == "BEAR_IMPULSE":
                        is_red = random.random() < 0.80
                        move = atr * random.uniform(0.5, 1.2) if is_red else atr * random.uniform(0.15, 0.4)
                        bar["close"] = round(bar["open"] - (move if is_red else -move), 2)
                        wick_u = round(atr * random.uniform(0.10, 0.30), 2)
                        wick_l = round(atr * random.uniform(0.10, 0.35), 2)
                        bar["volume"] = random.randint(15000, 45000)
                    elif w_state.phase == "BEAR_PULLBACK":
                        is_green = random.random() < 0.70
                        move = atr * random.uniform(0.3, 0.6) if is_green else atr * random.uniform(0.1, 0.3)
                        bar["close"] = round(bar["open"] + (move if is_green else -move), 2)
                        wick_u = round(atr * random.uniform(0.20, 0.40), 2)
                        wick_l = round(atr * random.uniform(0.10, 0.25), 2)
                        bar["volume"] = random.randint(5000, 18000)
                    elif w_state.phase == "REVERSAL_BOUNCE":
                        # Bullish Hammer! Long lower wick, small green body
                        move = atr * random.uniform(0.25, 0.6)
                        bar["close"] = round(bar["open"] + move, 2)
                        wick_u = round(atr * random.uniform(0.05, 0.20), 2)
                        wick_l = round(atr * random.uniform(1.4, 2.5), 2)  # Extended hammer shadow!
                        bar["volume"] = random.randint(25000, 60000)
                    else:  # RANGE_CONSOLIDATION
                        is_doji = random.random() < 0.45
                        move = atr * random.uniform(0.02, 0.15) if is_doji else atr * random.uniform(0.15, 0.4)
                        direction = 1 if random.random() < 0.50 else -1
                        bar["close"] = round(bar["open"] + direction * move, 2)
                        wick_u = round(atr * random.uniform(0.25, 0.65), 2)
                        wick_l = round(atr * random.uniform(0.25, 0.65), 2)
                        bar["volume"] = random.randint(4000, 15000)

                    bar["high"] = round(max(bar["open"], bar["close"]) + wick_u, 2)
                    bar["low"] = round(max(tick, min(bar["open"], bar["close"]) - wick_l), 2)
                else:
                    # Active traded bars: preserve authentic traded extremes from matched order flow
                    bar["high"] = max(bar["high"], max(bar["open"], bar["close"]))
                    bar["low"] = min(bar["low"], min(bar["open"], bar["close"]))

                    # Ensure active bar has realistic body and upper/lower shadows
                    min_tr = max(tick * 4, atr * 0.35)
                    body_top = max(bar["open"], bar["close"])
                    body_bot = min(bar["open"], bar["close"])

                    # If high - low is too compressed, synthesize natural bid-ask bounce shadows
                    if (bar["high"] - bar["low"]) < min_tr:
                        bar["high"] = round(body_top + atr * 0.20, 2)
                        bar["low"] = round(max(tick, body_bot - atr * 0.20), 2)
                    bar["volume"] = max(bar["volume"], random.randint(8000, 35000))

                self.current_mid[sym] = bar["close"]
                self.current_mid[sym.split("-")[0]] = bar["close"]

                # Recognize candlestick pattern from recent bars context
                recent_bars = [
                    {"open": c.open, "high": c.high, "low": c.low, "close": c.close}
                    for c in self.candles_cache.get(sym, [])[-3:]
                ]
                recent_bars.append({"open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"]})
                detected = CandlestickPatternRecognizer.recognize(recent_bars)
                self.latest_pattern[sym] = detected
                self.latest_pattern[sym.split("-")[0]] = detected

                pattern_model = self.pattern_models.get(sym)
                rsi_val = pattern_model.rsi if pattern_model else 50.0

                # Store completed candle with pattern in SQLite
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
                    pattern=detected.name,
                    pattern_sentiment=detected.sentiment,
                    pattern_score=detected.pattern_score,
                    rsi=rsi_val,
                    vwap=bar["close"],
                )

                # Record pattern detection event
                if detected.name not in ("NORMAL_BAR", "INSUFFICIENT_DATA"):
                    self.data_store.record_pattern_detection(
                        symbol=sym,
                        timestamp=int(now),
                        date_str=bar["date"],
                        pattern_name=detected.name,
                        category=detected.category,
                        sentiment=detected.sentiment,
                        confidence=detected.confidence,
                        pattern_score=detected.pattern_score,
                        description=detected.description,
                    )

                # Record feature snapshot into operational Feature Store
                if pattern_model:
                    pred = pattern_model.predict()
                    self.data_store.record_feature_vector(
                        symbol=sym,
                        timestamp=int(now),
                        date_str=bar["date"],
                        open_p=bar["open"],
                        high_p=bar["high"],
                        low_p=bar["low"],
                        close_p=bar["close"],
                        volume=bar["volume"],
                        pattern=detected.name,
                        pattern_sentiment=detected.sentiment,
                        pattern_score=detected.pattern_score,
                        rsi14=pattern_model.rsi,
                        macd_signal=pattern_model.predicted_drift * 100.0,
                        bb_bandwidth=pattern_model.bollinger_bandwidth,
                        atr_ratio=1.0,
                        mom5=pred.predicted_drift * 10.0,
                        mom10=pred.predicted_drift * 20.0,
                        vol_ratio=1.0,
                        vol10=daily_vol * 100.0,
                        price_accel=0.0,
                        predicted_regime=pred.regime.value,
                        confidence=pred.confidence,
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
