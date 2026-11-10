"""
FluxCore: Interactive Web GUI Server & Multi-Symbol Trading Gateway.
FastAPI + WebSocket backend providing real-time Level 2 market depth,
live order placement, multi-asset matching router, risk checks, and automated simulation.
"""

import asyncio
from contextlib import asynccontextmanager
import csv
import io
import os
from pathlib import Path
import random
import sys
import time
from typing import Dict, List, Optional, Any, Union

# On Windows, suppress spurious ConnectionResetError [WinError 10054] when browser closes or refreshes tab
if sys.platform == "win32":
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
        _orig_call_conn_lost = _ProactorBasePipeTransport._call_connection_lost

        def _silent_call_conn_lost(self, exc=None):
            try:
                _orig_call_conn_lost(self, exc)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                pass

        _ProactorBasePipeTransport._call_connection_lost = _silent_call_conn_lost
    except Exception:
        pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from fluxcore.protocol import (
    OrderSide,
    OrderType,
    to_fixed_price,
    from_fixed_price,
    TradePacket,
)
from fluxcore.engine.order_book_cy import FastOrderBook
from fluxcore.engine.order_policies import (
    OrderPolicyEngine,
    TimeInForce,
    SelfTradeAction,
)
from fluxcore.risk.risk_engine import (
    PreTradeRiskEngine,
    RiskLimits,
)
from fluxcore.market.router import (
    SymbolInfo,
    SingleSymbolVenue,
    MultiBookRouter,
)
from fluxcore.monitor.metrics import LatencyProfiler
from fluxcore.monitor.whale_detector import WhaleDetector
from fluxcore.market.distribution import OrderDistributionGenerator
from fluxcore.ledger.audit_logger import AuditLedger
from fluxcore.persistence.wal import WriteAheadLog
from fluxcore.persistence.snapshot import SnapshotManager
from fluxcore.persistence.recovery import DeterministicRecoveryEngine
from fluxcore.market.data_loader import MarketDataLoader
from fluxcore.market.storage import MarketDataStore
from fluxcore.market.ml_impact_engine import MLMarketImpactEngine
from fluxcore.market.replayer import HistoricalMarketReplayer
from fluxcore.market.sor_network import MultiVenueNetwork
from fluxcore.market.nbbo import NBBOEngine
from fluxcore.algo import (
    AlgoTradingManager,
    BacktestEngine,
    BacktestResult,
    MonteCarloEngine,
    MonteCarloResult,
    SmartOrderRouter,
    RoutingPolicy,
    LatencyArbitrageEngine,
)


class OrderRequest(BaseModel):
    side: str               # "BUY" or "SELL"
    order_type: str = "LIMIT"  # "LIMIT" or "MARKET"
    price: float = 150.00
    quantity: int = 100
    symbol: str = "SOL-USD" # Target instrument ticker
    # Phase 1: Advanced Order Type fields
    tif: str = "GTC"        # "GTC" | "IOC" | "FOK"
    post_only: bool = False  # Maker-only: reject if would cross spread
    stp: str = "CANCEL_AGGRESSOR"  # "ALLOW" | "CANCEL_AGGRESSOR" | "CANCEL_RESTING" | "CANCEL_BOTH"


class CancelRequest(BaseModel):
    order_id: int
    symbol: Optional[str] = None


class BotSpeedRequest(BaseModel):
    speed: str  # "slow", "medium", "fast", "hft"


class SymbolSelectRequest(BaseModel):
    symbol: str


class RLOrderRequest(BaseModel):
    symbol: Optional[str] = None
    side: str = "BUY"
    shares: int = 1000
    horizon_sec: float = 60.0
    risk_aversion: float = 0.001


class BacktestRunRequest(BaseModel):
    symbol: str = "NVDA"
    strategy: str = "dual_ema"
    initial_capital: float = 100_000.0
    max_bars: int = 300
    commission_bps: float = 5.0
    slippage_bps: float = 3.5
    stop_loss_pct: float = 4.0
    take_profit_pct: float = 8.0
    params: Optional[Dict[str, Any]] = None
    run_monte_carlo: bool = True
    monte_carlo_sims: int = 1_000


class MonteCarloRunRequest(BaseModel):
    symbol: str = "NVDA"
    num_simulations: int = 1_000
    forecast_bars: int = 60
    initial_equity: float = 100_000.0
    ruin_drawdown_pct: float = 20.0
    method: str = "bootstrap"


class SORRouteRequest(BaseModel):
    symbol: str = "NVDA"
    side: str = "BUY"
    quantity: int = 1000
    policy: str = "NBBO_BEST_PRICE"  # "NBBO_BEST_PRICE", "PRO_RATA_DEPTH", "COST_AND_FEE_OPTIMIZED", "SIMULTANEOUS_SWEEP"
    limit_price: Optional[float] = None


class MarketShockRequest(BaseModel):
    symbol: str = "NVDA"
    venue_id: str = "BATS"
    price_delta: float = 0.50



def is_serverless_env() -> bool:
    """Robust detection of serverless read-only container environments (Vercel, AWS Lambda)."""
    if os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV") or os.environ.get("IS_SERVERLESS"):
        return True
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("LAMBDA_TASK_ROOT") or os.environ.get("AWS_EXECUTION_ENV"):
        return True
    if os.path.exists("/var/task"):
        return True
    try:
        test_file = os.path.join(os.getcwd(), ".writable_test")
        with open(test_file, "w") as f:
            f.write("1")
        os.remove(test_file)
        return False
    except (OSError, PermissionError):
        return True


class WebTradingServer:
    def __init__(
        self,
        db_path: str = "fluxcore_audit.db",
        wal_path: str = "chronos_journal.wal",
        snapshot_dir: str = "chronos_snapshots",
        enable_wal: bool = False,
    ):
        if is_serverless_env():
            import tempfile
            tmp = tempfile.gettempdir()
            if not os.path.isabs(db_path) and db_path != ":memory:":
                db_path = os.path.join(tmp, os.path.basename(db_path))
            if not os.path.isabs(wal_path):
                wal_path = os.path.join(tmp, os.path.basename(wal_path))
            if not os.path.isabs(snapshot_dir):
                snapshot_dir = os.path.join(tmp, os.path.basename(snapshot_dir))

        self.router = MultiBookRouter()
        self.ledger = AuditLedger(db_path=db_path)
        self.risk_engine = PreTradeRiskEngine()
        self.active_symbol = "SOL-USD"
        self.enable_wal = enable_wal
        self.wal = WriteAheadLog(filepath=wal_path, sync_mode="batched") if enable_wal else None
        self.snapshot_mgr = SnapshotManager(snapshot_dir=snapshot_dir)

        # Register standard multi-asset instruments
        self.router.register_symbol(SymbolInfo(1, "SOL-USD", "SOL", "USD", 0.01, 1, 150.00))
        self.router.register_symbol(SymbolInfo(2, "BTC-USD", "BTC", "USD", 0.50, 1, 65000.00))
        self.router.register_symbol(SymbolInfo(3, "ETH-USD", "ETH", "USD", 0.10, 1, 3500.00))
        self.router.register_symbol(SymbolInfo(4, "NVDA-USD", "NVDA", "USD", 0.01, 1, 130.00))

        # Fund accounts with cash and multi-asset positions
        acc1 = self.risk_engine.get_or_create_account(1, initial_cash=1_000_000.0, initial_shares=500, symbol_id=1)
        acc1.deposit(shares=5, symbol_id=2)     # 5 BTC
        acc1.deposit(shares=50, symbol_id=3)    # 50 ETH
        acc1.deposit(shares=500, symbol_id=4)   # 500 NVDA

        self.risk_engine.get_or_create_account(2, initial_cash=10_000_000.0, initial_shares=100_000, symbol_id=1)
        self.risk_engine.get_or_create_account(99, initial_cash=100_000_000.0, initial_shares=1_000_000, symbol_id=1)

        self.bot_active = True
        self.bot_speed = "medium"
        self.bot_delay = 0.04  # ~25 ord/s
        self._bot_task: Optional[asyncio.Task] = None
        self.connected_clients: List[WebSocket] = []
        self.is_running = True

        # Real-Market Data Replayer & ML Pattern Impact Engine
        replayer_store = MarketDataStore(db_path=":memory:") if db_path == ":memory:" else None
        self.replayer = HistoricalMarketReplayer(
            router=self.router,
            data_store=replayer_store,
            default_ticker="NVDA",
            risk_engine=self.risk_engine,
        )
        for sym in ("NVDA", "NVDA-USD", "AAPL", "AAPL-USD", "MSFT", "MSFT-USD", "TSLA", "TSLA-USD", "RELIANCE", "RELIANCE-BSE", "BTC-USD", "ETH-USD", "SOL-USD"):
            try:
                self.replayer.bind_stock(sym)
            except Exception:
                pass

        # Provision positions across all bound equity & crypto instruments
        acc2 = self.risk_engine.get_or_create_account(2)
        acc99 = self.risk_engine.get_or_create_account(99)
        for venue in self.router.venues_by_id.values():
            s_id = venue.info.symbol_id
            if s_id not in acc1.positions or acc1.positions[s_id] < 100:
                acc1.deposit(shares=1000, symbol_id=s_id)
            if s_id not in acc2.positions:
                acc2.deposit(shares=100_000, symbol_id=s_id)
            if s_id not in acc99.positions:
                acc99.deposit(shares=1_000_000, symbol_id=s_id)

        # Institutional Quantitative Backtesting & Monte Carlo Engine
        self.backtest_engine = BacktestEngine(data_loader=self.replayer.data_loader)

        # Multi-Exchange Smart Order Routing (SOR) & HFT Latency Arbitrage
        self.sor_network = MultiVenueNetwork()
        self.nbbo_engine = NBBOEngine(network=self.sor_network)
        self.sor = SmartOrderRouter(network=self.sor_network, nbbo_engine=self.nbbo_engine)
        self.arbitrage_engine = LatencyArbitrageEngine(network=self.sor_network, nbbo_engine=self.nbbo_engine)

        # Seed multi-exchange liquidity across equity & crypto instruments
        seed_prices = {
            "NVDA": 130.00,
            "AAPL": 220.00,
            "MSFT": 415.00,
            "TSLA": 250.00,
            "RELIANCE": 2900.00,
            "BTC-USD": 65000.00,
            "ETH-USD": 3500.00,
            "SOL-USD": 150.00,
        }
        for s_sym, s_px in seed_prices.items():
            self.sor_network.seed_network(symbol=s_sym, mid_price=s_px, spread_bps=5.0)

    # ── Backward-compatible venue delegations ──────────────────────────────

    @property
    def book(self) -> FastOrderBook:
        return self.router.get_venue(self.active_symbol).book

    @property
    def profiler(self) -> LatencyProfiler:
        return self.router.get_venue(self.active_symbol).profiler

    @property
    def whale_detector(self) -> WhaleDetector:
        return self.router.get_venue(self.active_symbol).whale_detector

    @property
    def generator(self) -> OrderDistributionGenerator:
        return self.router.get_venue(self.active_symbol).generator

    @property
    def recent_trades(self) -> List[Dict[str, Any]]:
        return self.router.get_venue(self.active_symbol).recent_trades

    @property
    def price_history(self) -> List[Dict[str, Any]]:
        return self.router.get_venue(self.active_symbol).price_history

    @property
    def user_open_orders(self) -> Dict[int, Dict[str, Any]]:
        return self.router.get_venue(self.active_symbol).user_open_orders

    @property
    def total_orders(self) -> int:
        return self.router.get_venue(self.active_symbol).total_orders

    @total_orders.setter
    def total_orders(self, val: int):
        self.router.get_venue(self.active_symbol).total_orders = val

    @property
    def total_trades(self) -> int:
        return self.router.get_venue(self.active_symbol).total_trades

    @property
    def total_volume(self) -> int:
        return self.router.get_venue(self.active_symbol).total_volume

    @property
    def last_trade_price(self) -> float:
        return self.router.get_venue(self.active_symbol).last_trade_price

    @property
    def high_price(self) -> float:
        return self.router.get_venue(self.active_symbol).high_price

    @property
    def low_price(self) -> float:
        return self.router.get_venue(self.active_symbol).low_price

    @property
    def cumulative_notional(self) -> float:
        return self.router.get_venue(self.active_symbol).cumulative_notional

    @property
    def order_id_counter(self) -> int:
        return self.router.order_id_counter

    @order_id_counter.setter
    def order_id_counter(self, val: int):
        self.router.order_id_counter = val

    def seed_initial_liquidity(self):
        """No-op: Liquidity is seeded inside self.router.register_symbol()."""
        pass

    def place_order(
        self,
        side_str: str,
        type_str: str,
        price_float: float,
        quantity: int,
        client_id: int = 1,
        tif_str: str = "GTC",
        post_only: bool = False,
        stp_str: str = "CANCEL_AGGRESSOR",
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute an order through the multi-symbol router, risk engine, and policy layer."""
        venue = self.router.get_venue(symbol or self.active_symbol)
        sym_id = venue.info.symbol_id
        side = OrderSide.BUY if side_str.upper() == "BUY" else OrderSide.SELL
        order_id = self.router.allocate_order_id()
        self.router.client_order_registry[order_id] = client_id
        venue.total_orders += 1

        # For Market sweeps, set aggressive price crossing the current book without collar blowouts
        if type_str.upper() == "MARKET":
            best_bid_p = from_fixed_price(venue.book.best_bid_price)
            best_ask_p = from_fixed_price(venue.book.best_ask_price)
            curr_ref = best_ask_p if (best_ask_p > 0 and side == OrderSide.BUY) else (best_bid_p if best_bid_p > 0 else (venue.last_trade_price or venue.info.initial_price))
            collar = getattr(self.risk_engine.limits, "price_collar_pct", 0.10) or 0.10
            crossing_factor = min(collar * 0.8, 0.05)
            if side == OrderSide.BUY:
                exec_price = to_fixed_price(curr_ref * (1.0 + crossing_factor))
            else:
                exec_price = to_fixed_price(curr_ref * (1.0 - crossing_factor))
        else:
            exec_price = to_fixed_price(price_float)

        # Resolve TIF enum
        tif_map = {"GTC": TimeInForce.GTC, "IOC": TimeInForce.IOC, "FOK": TimeInForce.FOK}
        tif = tif_map.get(tif_str.upper(), TimeInForce.GTC)

        # Resolve STP enum
        stp_map = {
            "ALLOW":             SelfTradeAction.ALLOW,
            "CANCEL_AGGRESSOR":  SelfTradeAction.CANCEL_AGGRESSOR,
            "CANCEL_RESTING":    SelfTradeAction.CANCEL_RESTING,
            "CANCEL_BOTH":       SelfTradeAction.CANCEL_BOTH,
        }
        stp_action = stp_map.get(stp_str.upper(), SelfTradeAction.CANCEL_AGGRESSOR)

        t0_ns = time.perf_counter_ns()

        # [Pre-Trade Risk Gatekeeping Check]
        best_bid_p = from_fixed_price(venue.book.best_bid_price)
        best_ask_p = from_fixed_price(venue.book.best_ask_price)
        risk_decision = self.risk_engine.validate_order(
            order_id=order_id,
            client_id=client_id,
            side=int(side),
            price_float=price_float,
            quantity=quantity,
            symbol_id=sym_id,
            best_bid_float=best_bid_p,
            best_ask_float=best_ask_p,
            is_market=(type_str.upper() == "MARKET"),
            timestamp_ns=t0_ns,
        )

        if not risk_decision.passed:
            t1_ns = time.perf_counter_ns()
            lat_ns = t1_ns - t0_ns
            venue.profiler.record_delta(t0_ns, t1_ns)
            return {
                "success": False,
                "order_id": order_id,
                "symbol": venue.info.ticker,
                "side": side_str.upper(),
                "type": type_str.upper(),
                "tif": tif.name,
                "post_only": post_only,
                "stp": stp_action.name,
                "price": price_float,
                "quantity": quantity,
                "total_filled": 0,
                "matched_trades": [],
                "cancelled_remainder": 0,
                "stp_cancelled_qty": 0,
                "reject_reason": risk_decision.reject_reason,
                "latency_us": lat_ns / 1000.0,
            }

        # Market orders are IOC by convention — fill what's available, cancel rest
        if type_str.upper() == "MARKET" and tif == TimeInForce.GTC:
            tif = TimeInForce.IOC

        # Append to Write-Ahead Log before engine matching
        if self.enable_wal and self.wal:
            self.wal.append_order_submit(
                order_id=order_id,
                client_id=client_id,
                symbol_id=sym_id,
                side=int(side),
                price=exec_price,
                quantity=quantity,
                tif=int(tif),
                post_only=int(post_only),
                stp_action=int(stp_action),
                timestamp_ns=t0_ns,
            )

        policy_result = venue.policy_engine.apply(
            order_id=order_id,
            client_id=client_id,
            price_fixed=exec_price,
            quantity=quantity,
            side=int(side),
            symbol_id=sym_id,
            timestamp_ns=t0_ns,
            tif=tif,
            post_only=post_only,
            stp_action=stp_action,
        )

        t1_ns = time.perf_counter_ns()
        lat_ns = t1_ns - t0_ns
        venue.profiler.record_delta(t0_ns, t1_ns)

        # If rejected by policy (FOK/POST_ONLY), return early with rejection info
        if not policy_result.accepted:
            return {
                "success": False,
                "order_id": order_id,
                "symbol": venue.info.ticker,
                "side": side_str.upper(),
                "type": type_str.upper(),
                "tif": tif.name,
                "post_only": post_only,
                "stp": stp_action.name,
                "price": price_float,
                "quantity": quantity,
                "total_filled": 0,
                "matched_trades": [],
                "cancelled_remainder": 0,
                "stp_cancelled_qty": 0,
                "reject_reason": policy_result.reject_reason,
                "latency_us": lat_ns / 1000.0,
            }

        matches = policy_result.matches
        filled_shares = policy_result.total_filled
        matched_info = []

        for m in matches:
            qty = m["quantity"]
            venue.total_trades += 1
            venue.total_volume += qty
            trade_p = from_fixed_price(m["execution_price"])
            venue.last_trade_price = trade_p
            venue.high_price = max(venue.high_price, trade_p)
            venue.low_price = min(venue.low_price, trade_p)
            venue.cumulative_notional += trade_p * qty

            tp = TradePacket(
                trade_id=m["trade_id"],
                buy_order_id=m["buy_order_id"],
                sell_order_id=m["sell_order_id"],
                price=m["execution_price"],
                quantity=qty,
                symbol_id=sym_id,
                aggressor_side=m["aggressor_side"],
                match_timestamp_ns=t1_ns,
                latency_ns=lat_ns,
            )
            venue.whale_detector.inspect_trade(tp)
            self.ledger.insert_trade(tp)

            trade_data = {
                "trade_id": tp.trade_id,
                "symbol": venue.info.ticker,
                "side": "BUY" if m["aggressor_side"] == int(OrderSide.BUY) else "SELL",
                "price": trade_p,
                "quantity": qty,
                "latency_us": lat_ns / 1000.0,
                "time": time.strftime("%H:%M:%S"),
            }
            matched_info.append(trade_data)
            venue.recent_trades.insert(0, trade_data)

            # Settle trade in Risk Engine
            buy_c = self.router.client_order_registry.get(m["buy_order_id"], 99)
            sell_c = self.router.client_order_registry.get(m["sell_order_id"], 99)
            self.risk_engine.on_trade_executed(
                buy_client_id=buy_c,
                sell_client_id=sell_c,
                price_float=trade_p,
                quantity=qty,
                symbol_id=sym_id,
                buy_order_id=m["buy_order_id"],
                sell_order_id=m["sell_order_id"],
            )

            # ML Market Impact & Pattern feedback: dynamic price & spread reaction to buy/sell pressure
            impact = self.replayer.impact_engines.get(venue.info.ticker)
            if impact:
                impact.record_execution(int(side), qty, trade_p)

            pattern_model = self.replayer.pattern_models.get(venue.info.ticker)
            if pattern_model:
                pattern_model.record_trade(trade_p, qty, int(side))
                best_bid_p = from_fixed_price(venue.book.best_bid_price)
                best_ask_p = from_fixed_price(venue.book.best_ask_price)
                pattern_model.update_order_book_depth(
                    best_bid_p, best_ask_p,
                    venue.book.best_bid_volume, venue.book.best_ask_volume,
                )

            # Persist tick into MarketDataStore
            best_bid_p = from_fixed_price(venue.book.best_bid_price)
            best_ask_p = from_fixed_price(venue.book.best_ask_price)
            self.replayer.data_store.record_tick(
                symbol=venue.info.ticker,
                timestamp_ns=time.perf_counter_ns(),
                price=trade_p,
                quantity=qty,
                side=int(side),
                best_bid=best_bid_p,
                best_ask=best_ask_p,
                ofi=impact.ofi if impact else 0.0,
                regime=pattern_model.current_regime.value if pattern_model else "BALANCED_CONSOLIDATION",
            )
            self.replayer._update_live_candle(venue.info.ticker, trade_p, qty)

            # Algorithmic Trading fill notification
            algo_mgr = self.replayer.algo_managers.get(venue.info.ticker)
            if algo_mgr:
                algo_mgr.on_trade_executed(buy_c, int(OrderSide.BUY), trade_p, qty)
                algo_mgr.on_trade_executed(sell_c, int(OrderSide.SELL), trade_p, qty)

        # Apply market impact, price continuation, MM inventory shift, and wave breakout
        if filled_shares > 0:
            total_notional = sum(from_fixed_price(m["execution_price"]) * m["quantity"] for m in matches)
            avg_exec_p = round(total_notional / filled_shares, 2)
            self.replayer.apply_user_trade_impact(
                symbol=venue.info.ticker,
                side=int(side),
                quantity=filled_shares,
                execution_price=avg_exec_p,
            )

        # Record price tick for live chart
        if matches:
            venue.price_history.append({
                "time": time.strftime("%H:%M:%S"),
                "price": venue.last_trade_price,
                "volume": filled_shares,
            })
            if len(venue.price_history) > 100:
                venue.price_history = venue.price_history[-100:]

        if len(venue.recent_trades) > 30:
            venue.recent_trades = venue.recent_trades[:30]

        # Track user resting orders for cancellation
        if client_id == 1 and type_str.upper() == "LIMIT" and tif == TimeInForce.GTC:
            rem = quantity - filled_shares
            if rem > 0:
                self.risk_engine.on_order_accepted(
                    order_id=order_id,
                    client_id=client_id,
                    side=int(side),
                    price_float=price_float,
                    quantity=rem,
                    symbol_id=sym_id,
                )
                venue.user_open_orders[order_id] = {
                    "order_id": order_id,
                    "symbol": venue.info.ticker,
                    "side": side_str.upper(),
                    "price": price_float,
                    "quantity": quantity,
                    "filled": filled_shares,
                    "remaining": rem,
                    "tif": tif.name,
                    "post_only": post_only,
                    "time": time.strftime("%H:%M:%S"),
                }
                pattern_model = self.replayer.pattern_models.get(venue.info.ticker)
                if pattern_model:
                    pattern_model.update_order_book_depth(
                        from_fixed_price(venue.book.best_bid_price),
                        from_fixed_price(venue.book.best_ask_price),
                        venue.book.best_bid_volume,
                        venue.book.best_ask_volume,
                    )

        return {
            "success": True,
            "order_id": order_id,
            "symbol": venue.info.ticker,
            "side": side_str.upper(),
            "type": type_str.upper(),
            "tif": tif.name,
            "post_only": post_only,
            "stp": stp_action.name,
            "price": price_float,
            "quantity": quantity,
            "total_filled": filled_shares,
            "matched_trades": matched_info,
            "cancelled_remainder": policy_result.cancelled_remainder,
            "stp_cancelled_qty": policy_result.stp_cancelled_qty,
            "reject_reason": None,
            "latency_us": lat_ns / 1000.0,
        }

    def cancel_user_order(self, order_id: int, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Instant O(1) order cancellation by order_id across venues."""
        venue = self.router.get_venue(symbol or self.active_symbol)
        if order_id not in venue.user_open_orders:
            # Search across all venues
            for v in self.router.venues_by_id.values():
                if order_id in v.user_open_orders:
                    venue = v
                    break

        t0 = time.perf_counter_ns()
        cancelled = venue.book.cancel_order(order_id)
        t1 = time.perf_counter_ns()
        lat_ns = t1 - t0
        venue.profiler.record_delta(t0, t1)

        if order_id in venue.user_open_orders:
            del venue.user_open_orders[order_id]

        if cancelled and self.enable_wal and self.wal:


            self.wal.append_order_cancel(


                order_id=order_id,


                client_id=self.router.client_order_registry.get(order_id, 1),


                symbol_id=venue.info.symbol_id,


                timestamp_ns=time.perf_counter_ns(),


            )


        self.risk_engine.on_order_cancelled(order_id=order_id, client_id=1)

        return {
            "success": cancelled,
            "order_id": order_id,
            "symbol": venue.info.ticker,
            "latency_us": lat_ns / 1000.0,
        }

    def get_snapshot(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Construct real-time market snapshot for the active or requested symbol."""
        sym = symbol or self.active_symbol
        try:
            venue = self.router.get_venue(sym)
        except KeyError:
            venue = self.replayer.bind_stock(sym)
        snap = venue.get_snapshot()

        snap["orders_per_sec"] = int(1.0 / max(0.001, self.bot_delay)) if self.bot_active else 0
        snap["bot_active"] = self.bot_active
        snap["bot_speed"] = self.bot_speed
        snap["risk"] = self.risk_engine.get_risk_metrics()
        snap["account"] = self.risk_engine.get_or_create_account(1).to_dict()
        snap["active_symbol"] = self.active_symbol
        snap["symbols"] = self.router.list_symbols()
        snap["tickers"] = self.router.get_all_tickers()
        impact = self.replayer.impact_engines.get(venue.info.ticker)
        snap["ml_impact"] = impact.to_dict() if impact else None
        pattern_model = self.replayer.pattern_models.get(venue.info.ticker)
        snap["ml_pattern"] = pattern_model.to_dict() if pattern_model else None
        snap["market_stats"] = self.replayer.data_store.get_market_statistics(venue.info.ticker)
        algo_mgr = self.replayer.algo_managers.get(venue.info.ticker)
        snap["algo_stats"] = algo_mgr.get_summary(venue.last_trade_price) if algo_mgr else None
        snap["candles"] = self.replayer.get_candlesticks(venue.info.ticker, limit=180)
        pat = self.replayer.latest_pattern.get(venue.info.ticker)
        snap["pattern"] = {
            "name": pat.name,
            "category": pat.category,
            "sentiment": pat.sentiment,
            "confidence": pat.confidence,
            "pattern_score": pat.pattern_score,
            "description": pat.description,
        } if pat else None
        return snap


def get_index_html_path() -> Path:
    """Find index.html across local development and serverless bundle directories."""
    candidates = [
        Path(__file__).parent / "index.html",
        Path(__file__).resolve().parent / "index.html",
        Path(os.getcwd()) / "fluxcore" / "web" / "index.html",
        Path(os.getcwd()) / "api" / "index.html",
        Path(__file__).resolve().parents[2] / "fluxcore" / "web" / "index.html",
        Path(__file__).resolve().parents[2] / "api" / "index.html",
        Path("/var/task/fluxcore/web/index.html"),
        Path("/var/task/api/index.html"),
    ]
    for p in candidates:
        try:
            if p.is_file():
                return p
        except Exception:
            pass
    return candidates[0]


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Auto-start background living market simulation loop on server startup."""
    is_serverless = is_serverless_env()
    server.is_running = True
    # In serverless environments, do not spawn infinite loops that block request completion
    if not is_serverless and server.bot_active:
        if server._bot_task is None or server._bot_task.done():
            server._bot_task = asyncio.create_task(_bot_simulation_loop())
    yield
    # Clean shutdown on server exit:
    server.is_running = False
    if server._bot_task and not server._bot_task.done():
        server._bot_task.cancel()
    for client in list(server.connected_clients):
        try:
            asyncio.create_task(client.close())
        except Exception:
            pass
    server.connected_clients.clear()


# Initialize App & Server instance
server = WebTradingServer()
app = FastAPI(title="FluxCore Multi-Asset Trading Terminal", lifespan=lifespan)

# Allow cross-origin requests (e.g. from file:// or other local ports)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Return empty 204 No Content for favicon requests to prevent 404 logs."""
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve single-page trading terminal with strict no-cache headers."""
    index_path = get_index_html_path()
    if not index_path.exists():
        raise HTTPException(status_code=404, detail=f"index.html not found (checked: {index_path})")
    content = index_path.read_text(encoding="utf-8")
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/snapshot")
async def api_snapshot(symbol: Optional[str] = None):
    """Fetch current market snapshot for active or specified symbol."""
    is_serverless = is_serverless_env()
    if is_serverless and server.bot_active:
        # Step market tick on-demand on each frontend snapshot poll in serverless environments
        try:
            target_sym = symbol or server.active_symbol
            server.replayer.step_market_tick(target_sym)
        except Exception:
            pass
    return server.get_snapshot(symbol=symbol)


@app.post("/api/order")
async def api_order(req: OrderRequest):
    """Place a new interactive Buy/Sell order with multi-symbol routing."""
    res = server.place_order(
        side_str=req.side,
        type_str=req.order_type,
        price_float=req.price,
        quantity=req.quantity,
        symbol=req.symbol,
        tif_str=req.tif,
        post_only=req.post_only,
        stp_str=req.stp,
    )
    return res


@app.get("/api/symbols")
async def api_symbols():
    """Retrieve metadata for all tradeable financial instruments."""
    return server.router.list_symbols()


@app.get("/api/tickers")
async def api_tickers():
    """Retrieve 24h market metrics across all symbols."""
    return server.router.get_all_tickers()


@app.post("/api/symbol")
@app.post("/api/symbol/select")
async def api_select_symbol(req: SymbolSelectRequest):
    """Switch active market symbol."""
    try:
        venue = server.router.get_venue(req.symbol)
    except KeyError:
        venue = server.replayer.bind_stock(req.symbol)
    server.active_symbol = venue.info.ticker
    return {"active_symbol": server.active_symbol, "symbol": venue.info.ticker}


@app.post("/api/cancel")
async def api_cancel(req: CancelRequest):
    """Cancel a resting order by order_id."""
    return server.cancel_user_order(req.order_id, symbol=req.symbol)


@app.post("/api/bot/speed")
async def api_bot_speed(req: BotSpeedRequest):
    """Set market simulation bot speed."""
    server.bot_speed = req.speed
    if req.speed == "slow":
        server.bot_delay = 0.20   # 5 ord/s
    elif req.speed == "fast":
        server.bot_delay = 0.01   # 100 ord/s
    elif req.speed == "hft":
        server.bot_delay = 0.002  # 500 ord/s
    else:
        server.bot_delay = 0.04   # 25 ord/s (medium)
    return {"speed": req.speed, "delay": server.bot_delay}


@app.post("/api/bot/toggle")
async def api_toggle_bot():
    """Toggle background liquidity simulation across instruments."""
    server.bot_active = not server.bot_active
    is_serverless = is_serverless_env()
    if not is_serverless and server.bot_active:
        if server._bot_task is None or server._bot_task.done():
            server._bot_task = asyncio.create_task(_bot_simulation_loop())
    return {
        "bot_active": server.bot_active,
        "active": server.bot_active,
        "speed": server.bot_speed,
    }


async def _bot_simulation_loop():
    """Generates continuous realistic background order flow across all active venues."""
    while server.bot_active and getattr(server, "is_running", True):
        try:
            # 1. Prioritize active symbol currently viewed by the trader
            active_sym = server.active_symbol
            server.replayer.step_market_tick(active_sym)

            # 2. Also step a secondary venue in the background
            venues = list(server.router.venues_by_id.values())
            if venues:
                bg_venue = random.choice(venues)
                if bg_venue.info.ticker != active_sym:
                    server.replayer.step_market_tick(bg_venue.info.ticker)

            await asyncio.sleep(server.bot_delay)
        except asyncio.CancelledError:
            break
        except Exception:
            traceback.print_exc()
            await asyncio.sleep(0.05)


@app.get("/api/account")
async def api_account():
    """Retrieve Client #1 clearinghouse margin and position inventory."""
    return server.risk_engine.get_or_create_account(1).to_dict()


@app.get("/api/risk/metrics")
async def api_risk_metrics():
    """Retrieve pre-trade risk telemetry and blocked order statistics."""
    return server.risk_engine.get_risk_metrics()


@app.post("/api/risk/circuit-breaker/toggle")
async def api_toggle_circuit_breaker():
    """Emergency administrative circuit breaker toggle."""
    if server.risk_engine.limits.circuit_breaker_active:
        server.risk_engine.reset_circuit_breaker()
    else:
        server.risk_engine.trip_circuit_breaker()
    return {"circuit_breaker_active": server.risk_engine.limits.circuit_breaker_active}


@app.post("/api/snapshot/create")
async def api_create_snapshot():
    """Checkpoint matching engine state to disk."""
    seq = server.wal.next_seq - 1 if server.wal else 0
    snap_path = server.snapshot_mgr.create_snapshot(
        router=server.router,
        last_seq=seq,
        risk_engine=server.risk_engine,
    )
    if server.enable_wal and server.wal:
        server.wal.append_snapshot_marker(seq, snap_path)
    return {"success": True, "snapshot_path": snap_path, "seq": seq}


@app.get("/api/wal/status")
async def api_wal_status():
    """Retrieve Write-Ahead Log status and sequence telemetry."""
    seq = server.wal.next_seq if server.wal else 0
    size_bytes = os.path.getsize(server.wal.filepath) if server.wal and os.path.exists(server.wal.filepath) else 0
    return {
        "enabled": server.enable_wal,
        "next_seq": seq,
        "wal_file": server.wal.filepath if server.wal else None,
        "file_size_bytes": size_bytes,
    }


@app.get("/api/market/export-csv")
def api_export_market_csv(
    symbol: Optional[str] = None,
    duration: str = "2h",
    data_type: str = "trades",
):
    """
    Export comprehensive market buy & sell order execution tape and OHLCV candlestick data
    in institutional CSV format. Supports duration filters: 30m, 1h, 2h (max 2 hours), or all session,
    and supports exporting all stocks combined (symbol='ALL') or individual instruments.
    """
    raw_sym = (symbol or server.active_symbol or "ALL").strip().upper()
    dur_str = duration.strip().lower()
    
    # Map duration string to minutes
    dur_map = {
        "30m": 30,
        "30min": 30,
        "1h": 60,
        "60m": 60,
        "2h": 120,
        "120m": 120,
        "all": None,
    }
    dur_minutes = dur_map.get(dur_str, 120)  # default to 2h max
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Target symbols
    is_all = raw_sym in ("ALL", "*", "")
    target_sym_label = "ALL_STOCKS" if is_all else raw_sym
    
    if data_type.lower() == "candles":
        writer.writerow([
            "candle_id", "symbol", "timestamp", "date_str", "open", "high", "low", "close",
            "volume", "trades_count", "pattern", "pattern_sentiment", "pattern_score", "rsi", "vwap"
        ])
        candles_rows = server.replayer.data_store.get_candles_for_export(
            symbol=None if is_all else raw_sym,
            duration_minutes=dur_minutes,
            limit=20000,
        )
        if candles_rows:
            for r in candles_rows:
                writer.writerow([
                    r.get("id", ""),
                    r.get("symbol", ""),
                    r.get("timestamp", ""),
                    r.get("date_str", ""),
                    f"{float(r.get('open', 0)):.2f}",
                    f"{float(r.get('high', 0)):.2f}",
                    f"{float(r.get('low', 0)):.2f}",
                    f"{float(r.get('close', 0)):.2f}",
                    r.get("volume", 0),
                    r.get("trades_count", 0),
                    r.get("pattern", "NORMAL"),
                    r.get("pattern_sentiment", "NEUTRAL"),
                    r.get("pattern_score", 0.0),
                    r.get("rsi", 50.0),
                    r.get("vwap", 0.0),
                ])
        else:
            # Fallback if DB query returned empty
            symbols_to_fetch = list(server.router.venues_by_ticker.keys()) if is_all else [raw_sym]
            c_id = 1
            for s in set(symbols_to_fetch):
                bars = server.replayer.get_candlesticks(s, limit=120 if dur_minutes == 120 else 60)
                for b in bars:
                    writer.writerow([
                        c_id, s, int(time.time()), b.get("date", ""),
                        f"{float(b.get('open', 0)):.2f}", f"{float(b.get('high', 0)):.2f}",
                        f"{float(b.get('low', 0)):.2f}", f"{float(b.get('close', 0)):.2f}",
                        b.get("volume", 0), 1, "NORMAL", "NEUTRAL", 0.0, 50.0, b.get("close", 0),
                    ])
                    c_id += 1
        filename = f"FluxCore_{target_sym_label}_{dur_str.upper()}_Candles.csv"
    else:
        # Default: Detailed Execution Trade Tape & Order Fills
        writer.writerow([
            "trade_id", "symbol", "timestamp_ns", "side", "price", "quantity",
            "notional", "best_bid", "best_ask", "spread", "ofi", "regime"
        ])
        ticks = server.replayer.data_store.get_trades_for_export(
            symbol=None if is_all else raw_sym,
            duration_minutes=dur_minutes,
            limit=100000,
        )
        if ticks:
            for t in ticks:
                p = float(t.get("price", 0.0))
                q = int(t.get("quantity", 0))
                side_str = "BUY" if t.get("side", 0) == 0 else "SELL"
                bb = float(t.get("best_bid", 0.0))
                ba = float(t.get("best_ask", 0.0))
                writer.writerow([
                    t.get("id", 0),
                    t.get("symbol", ""),
                    t.get("timestamp_ns", 0),
                    side_str,
                    f"{p:.2f}",
                    q,
                    f"{p * q:.2f}",
                    f"{bb:.2f}",
                    f"{ba:.2f}",
                    f"{max(0.0, ba - bb):.2f}",
                    f"{float(t.get('ofi', 0.0)):.2f}",
                    t.get("regime", "BALANCED_CONSOLIDATION"),
                ])
        else:
            # Fallback to in-memory recent trades if DB was empty
            now_t = time.time()
            symbols_to_fetch = list(server.router.venues_by_ticker.keys()) if is_all else [raw_sym]
            tid = 1
            for s in set(symbols_to_fetch):
                try:
                    venue = server.router.get_venue(s)
                    recent = venue.recent_trades or []
                    for t in recent:
                        p = float(t.get("price", 0.0))
                        q = int(t.get("quantity", 0))
                        writer.writerow([
                            tid, s, int(now_t * 1e9), t.get("side", "BUY"),
                            f"{p:.2f}", q, f"{p * q:.2f}",
                            f"{p - 0.05:.2f}", f"{p + 0.05:.2f}", "0.10", "0.0", "ACTIVE"
                        ])
                        tid += 1
                except Exception:
                    pass
        filename = f"FluxCore_{target_sym_label}_{dur_str.upper()}_Trades.csv"

    csv_data = output.getvalue()
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "no-cache",
        },
    )


@app.get("/api/market/export-features-csv")
async def api_export_features_csv(symbol: Optional[str] = None):
    """
    Export quantitative microstructure feature store (OFI, micro-price, volatility, VPIN, regime).
    Enables advanced quantitative backtesting and institutional machine learning analysis.
    """
    sym = (symbol or server.active_symbol).strip().upper()
    samples = server.replayer.data_store.get_feature_store_samples(sym, limit=5000)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "timestamp", "date_str", "symbol", "open", "high", "low", "close", "volume",
        "pattern", "pattern_sentiment", "pattern_score", "rsi14", "macd_signal",
        "bb_bandwidth", "atr_ratio", "mom5", "mom10", "vol_ratio", "vol10",
        "price_accel", "predicted_regime", "confidence"
    ])

    for s in samples:
        writer.writerow([
            s.get("timestamp", 0),
            s.get("date_str", ""),
            s.get("symbol", sym),
            f"{s.get('open', 0.0):.2f}",
            f"{s.get('high', 0.0):.2f}",
            f"{s.get('low', 0.0):.2f}",
            f"{s.get('close', 0.0):.2f}",
            s.get("volume", 0),
            s.get("pattern", "NORMAL_BAR"),
            s.get("pattern_sentiment", "NEUTRAL"),
            s.get("pattern_score", 0.0),
            s.get("rsi14", 50.0),
            s.get("macd_signal", 0.0),
            s.get("bb_bandwidth", 0.0),
            s.get("atr_ratio", 1.0),
            s.get("mom5", 0.0),
            s.get("mom10", 0.0),
            s.get("vol_ratio", 1.0),
            s.get("vol10", 0.0),
            s.get("price_accel", 0.0),
            s.get("predicted_regime", "BALANCED_CONSOLIDATION"),
            s.get("confidence", 0.85),
        ])

    csv_data = output.getvalue()
    filename = f"FluxCore_{sym}_Quant_Features.csv"
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "no-cache",
        },
    )


@app.get("/api/market/companies")
async def api_get_companies():
    """List popular global companies with real historical data available."""
    return server.replayer.data_loader.get_popular_companies()


@app.post("/api/market/select-company")
async def api_select_company(req: SymbolSelectRequest):
    """Load and bind real company stock data into the active venue."""
    ticker = req.symbol.strip().upper()
    venue = server.replayer.bind_stock(ticker)
    server.active_symbol = venue.info.ticker
    return {
        "success": True,
        "symbol": venue.info.ticker,
        "initial_price": venue.info.initial_price,
        "tick_size": venue.info.tick_size,
    }


@app.get("/api/market/candles")
async def api_get_candles(symbol: Optional[str] = None, limit: int = 100):
    """Retrieve real historical candlestick bars for charting."""
    sym = symbol or server.active_symbol
    return server.replayer.get_candlesticks(sym, limit=limit)


@app.get("/api/market/ml-impact")
async def api_get_ml_impact(symbol: Optional[str] = None):
    """Retrieve real-time ML market impact state."""
    sym = symbol or server.active_symbol
    impact = server.replayer.impact_engines.get(sym)
    return impact.to_dict() if impact else {}


@app.post("/api/algo/toggle")
async def api_toggle_algo(bot_type: str = Query("mm")):
    """Toggle auto-trading algorithm (mm for Market Maker, momentum for Breakout)."""
    sym = server.active_symbol
    algo_mgr = server.replayer.algo_managers.get(sym)
    if not algo_mgr:
        server.replayer.bind_stock(sym)
        algo_mgr = server.replayer.algo_managers.get(sym)
    active = algo_mgr.toggle_bot(bot_type)
    return {"bot_type": bot_type, "active": active}


@app.get("/api/algo/status")
async def api_algo_status(symbol: Optional[str] = None):
    """Retrieve live PnL and telemetry for auto-trading algorithms."""
    sym = symbol or server.active_symbol
    algo_mgr = server.replayer.algo_managers.get(sym)
    venue = server.router.get_venue(sym)
    mid = venue.last_trade_price or venue.info.initial_price
    return algo_mgr.get_summary(mid) if algo_mgr else {}


@app.post("/api/algo/rl/start-order")
def api_rl_start_order(req: RLOrderRequest):
    """Commission a new institutional parent order for RL optimal execution."""
    sym = (req.symbol or server.active_symbol).upper()
    algo_mgr = server.replayer.algo_managers.get(sym)
    if not algo_mgr:
        server.replayer.bind_stock(sym)
        algo_mgr = server.replayer.algo_managers.get(sym)
    venue = server.router.get_venue(sym)
    mid = venue.last_trade_price or venue.info.initial_price or 100.0

    order = algo_mgr.rl_execution_agent.start_parent_order(
        symbol=sym,
        side_str=req.side,
        total_shares=req.shares,
        horizon_sec=req.horizon_sec,
        arrival_price=mid,
        risk_aversion=req.risk_aversion,
    )
    return {
        "status": "ORDER_STARTED",
        "order": algo_mgr.rl_execution_agent.to_dict(mid),
    }


@app.get("/api/algo/rl/telemetry")
def api_rl_telemetry(symbol: Optional[str] = None):
    """Retrieve telemetry, execution progress, and Q-network loss for RL agent."""
    sym = (symbol or server.active_symbol).upper()
    algo_mgr = server.replayer.algo_managers.get(sym)
    if not algo_mgr:
        server.replayer.bind_stock(sym)
        algo_mgr = server.replayer.algo_managers.get(sym)
    venue = server.router.get_venue(sym)
    mid = venue.last_trade_price or venue.info.initial_price or 100.0
    return algo_mgr.rl_execution_agent.to_dict(mid)


@app.post("/api/algo/rl/cancel-order")
def api_rl_cancel_order(symbol: Optional[str] = None):
    """Stop the active RL parent order."""
    sym = (symbol or server.active_symbol).upper()
    algo_mgr = server.replayer.algo_managers.get(sym)
    if algo_mgr and algo_mgr.rl_execution_agent.active_parent_order:
        algo_mgr.rl_execution_agent._finish_parent_order()
    return {"status": "ORDER_CANCELLED"}


@app.get("/api/market/ml-pattern")
async def api_get_ml_pattern(symbol: Optional[str] = None):
    """Retrieve real-time Lightweight ML Market Pattern telemetry."""
    sym = symbol or server.active_symbol
    pattern = server.replayer.pattern_models.get(sym)
    return pattern.to_dict() if pattern else {}


@app.get("/api/market/stats")
async def api_get_market_stats(symbol: Optional[str] = None):
    """Retrieve stored session market data statistics."""
    sym = symbol or server.active_symbol
    return server.replayer.data_store.get_market_statistics(sym)


@app.get("/api/market/patterns")
async def api_get_market_patterns(symbol: Optional[str] = None, limit: int = 15):
    """Retrieve persisted detected candlestick patterns for a symbol."""
    sym = symbol or server.active_symbol
    return server.replayer.data_store.get_recent_patterns(sym, limit=limit)


@app.get("/api/market/features")
async def api_get_feature_samples(symbol: Optional[str] = None, limit: int = 200):
    """Retrieve operational feature store vectors for continuous ML learning."""
    sym = symbol or server.active_symbol
    return server.replayer.data_store.get_feature_store_samples(sym, limit=limit)


@app.get("/api/market/ml-metrics")
def api_get_ml_metrics():
    """Retrieve institutional multi-metric offline evaluation scorecard."""
    weights_path = Path(__file__).parent.parent / "market" / "ml_weights.json"
    if not weights_path.exists():
        weights_path = Path(__file__).parent.parent.parent / "data" / "ml_weights.json"
    if weights_path.exists():
        try:
            import json
            with open(weights_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "version": data.get("version"),
                "trained_at": data.get("trained_at"),
                "total_samples": data.get("total_samples"),
                "total_stocks": data.get("total_stocks"),
                "feature_names": data.get("feature_names"),
                "feature_snr": data.get("feature_snr"),
                "evaluation_metrics": data.get("evaluation_metrics"),
                "weights": {
                    "w_ofi": data.get("w_ofi"),
                    "w_price_trend": data.get("w_price_trend"),
                    "w_autocorr": data.get("w_autocorr"),
                },
            }
        except Exception as e:
            return {"error": str(e)}
    return {"status": "weights_not_found"}


# ── Institutional Quantitative Backtesting & Monte Carlo API ───────────

@app.get("/api/backtest/strategies")
def api_get_backtest_strategies():
    """Retrieve catalog of institutional quantitative alpha strategies."""
    return server.backtest_engine.get_supported_strategies()


@app.post("/api/backtest/run")
def api_run_backtest(req: BacktestRunRequest):
    """Execute historical strategy backtest with institutional tear sheet analytics."""
    try:
        result = server.backtest_engine.run_backtest(
            symbol=req.symbol,
            strategy_id=req.strategy,
            initial_capital=req.initial_capital,
            max_bars=req.max_bars,
            commission_bps=req.commission_bps,
            slippage_bps=req.slippage_bps,
            stop_loss_pct=req.stop_loss_pct,
            take_profit_pct=req.take_profit_pct,
            strategy_params=req.params,
            run_monte_carlo=req.run_monte_carlo,
            monte_carlo_sims=req.monte_carlo_sims,
        )
        return result.to_dict()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/monte-carlo/simulate")
def api_run_monte_carlo(req: MonteCarloRunRequest):
    """Run parametric/bootstrapped Monte Carlo forward return fan cone simulation."""
    try:
        # Load returns for symbol
        candles = server.replayer.data_loader.load_candles(req.symbol)
        if not candles or len(candles) < 10:
            raise HTTPException(status_code=400, detail=f"Insufficient candle history for {req.symbol}")
        closes = [c.close for c in candles]
        rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1] > 0]
        mc_res = server.backtest_engine.monte_carlo_engine.simulate_from_returns(
            returns=rets,
            num_simulations=req.num_simulations,
            forecast_bars=req.forecast_bars,
            initial_equity=req.initial_equity,
            ruin_drawdown_pct=req.ruin_drawdown_pct,
            method=req.method,
            symbol=req.symbol,
        )
        return mc_res.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Multi-Exchange Smart Order Routing (SOR) & Arbitrage API ───────────

@app.get("/api/sor/venues")
def api_get_sor_venues():
    """List connected exchange venues, fee schedules, and geographic latencies."""
    return server.sor_network.list_venues()


@app.get("/api/sor/nbbo")
def api_get_sor_nbbo(symbol: Optional[str] = None):
    """Retrieve real-time consolidated National Best Bid & Offer (NBBO) quote."""
    sym = (symbol or server.active_symbol).upper().replace("-USD", "")
    # Check if symbol has venue order books, fallback to NVDA
    try:
        nbbo = server.nbbo_engine.compute_nbbo(sym)
    except Exception:
        sym = "NVDA"
        nbbo = server.nbbo_engine.compute_nbbo(sym)
    return nbbo.to_dict()


@app.get("/api/sor/arbitrage/stats")
def api_get_arbitrage_stats():
    """Retrieve operational telemetry and recent snipes of HFT latency arbitrage bot."""
    return server.arbitrage_engine.get_statistics()


@app.post("/api/sor/arbitrage/toggle")
def api_toggle_arbitrage():
    """Enable or disable the autonomous cross-exchange latency arbitrage bot."""
    server.arbitrage_engine.active = not server.arbitrage_engine.active
    return {"active": server.arbitrage_engine.active}


@app.post("/api/sor/arbitrage/step")
def api_step_arbitrage(symbol: Optional[str] = None):
    """Trigger a manual online arbitrage scan-and-snipe tick."""
    sym = (symbol or "NVDA").upper().replace("-USD", "")
    target = server.arbitrage_engine.step_arbitrage_cycle(sym)
    return {
        "sniped": target is not None,
        "opportunity": target.to_dict() if target else None,
        "stats": server.arbitrage_engine.get_statistics(),
    }


@app.post("/api/sor/route-order")
def api_route_smart_order(req: SORRouteRequest):
    """Route parent order across fragmented venues using chosen SOR policy."""
    sym = req.symbol.upper().replace("-USD", "")
    try:
        result = server.sor.route_order(
            symbol=sym,
            side=req.side,
            quantity=req.quantity,
            policy=req.policy,
            limit_price=req.limit_price,
        )
        return result.to_dict()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/sor/market-shock")
def api_inject_market_shock(req: MarketShockRequest):
    """Inject order flow shock into a specific venue to induce an arbitrage window."""
    sym = req.symbol.upper().replace("-USD", "")
    try:
        server.sor_network.inject_market_shock(symbol=sym, venue_id=req.venue_id, price_delta=req.price_delta)
        nbbo = server.nbbo_engine.compute_nbbo(sym)
        opps = server.arbitrage_engine.scan_opportunities(sym)
        return {
            "status": "SHOCK_INJECTED",
            "symbol": sym,
            "venue_id": req.venue_id,
            "nbbo": nbbo.to_dict(),
            "opportunities_detected": len(opps),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """High-frequency WebSocket broadcasting live snapshots for the active symbol at ~16 FPS."""
    await ws.accept()
    is_serverless = is_serverless_env()
    if not is_serverless and server.bot_active and (server._bot_task is None or server._bot_task.done()):
        server._bot_task = asyncio.create_task(_bot_simulation_loop())
    server.connected_clients.append(ws)
    try:
        while getattr(server, "is_running", True):
            snapshot = server.get_snapshot()
            await ws.send_json(snapshot)
            await asyncio.sleep(0.06)  # 60 ms refresh (~16 FPS)
    except (WebSocketDisconnect, ConnectionResetError, ConnectionAbortedError, BrokenPipeError, asyncio.CancelledError):
        pass
    except Exception:
        pass
    finally:
        if ws in server.connected_clients:
            server.connected_clients.remove(ws)
        try:
            await ws.close()
        except Exception:
            pass
