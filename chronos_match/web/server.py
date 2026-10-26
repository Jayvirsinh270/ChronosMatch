"""
ChronosMatch: Interactive Web GUI Server & Multi-Symbol Trading Gateway.
FastAPI + WebSocket backend providing real-time Level 2 market depth,
live order placement, multi-asset matching router, risk checks, and automated simulation.
"""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import random
import time
from typing import Dict, List, Optional, Any, Union

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from chronos_match.protocol import (
    OrderSide,
    OrderType,
    to_fixed_price,
    from_fixed_price,
    TradePacket,
)
from chronos_match.engine.order_book_cy import FastOrderBook
from chronos_match.engine.order_policies import (
    OrderPolicyEngine,
    TimeInForce,
    SelfTradeAction,
)
from chronos_match.risk.risk_engine import (
    PreTradeRiskEngine,
    RiskLimits,
)
from chronos_match.market.router import (
    SymbolInfo,
    SingleSymbolVenue,
    MultiBookRouter,
)
from chronos_match.monitor.metrics import LatencyProfiler
from chronos_match.monitor.whale_detector import WhaleDetector
from chronos_match.market.distribution import OrderDistributionGenerator
from chronos_match.ledger.audit_logger import AuditLedger
from chronos_match.persistence.wal import WriteAheadLog
from chronos_match.persistence.snapshot import SnapshotManager
from chronos_match.persistence.recovery import DeterministicRecoveryEngine
from chronos_match.market.data_loader import MarketDataLoader
from chronos_match.market.ml_impact_engine import MLMarketImpactEngine
from chronos_match.market.replayer import HistoricalMarketReplayer
from chronos_match.algo import AlgoTradingManager


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


class WebTradingServer:
    def __init__(
        self,
        db_path: str = "chronos_audit.db",
        wal_path: str = "chronos_journal.wal",
        snapshot_dir: str = "chronos_snapshots",
        enable_wal: bool = False,
    ):
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
        acc1 = self.risk_engine.get_or_create_account(1, initial_cash=10_000_000.0, initial_shares=100_000, symbol_id=1)
        acc1.deposit(shares=50, symbol_id=2)     # 50 BTC
        acc1.deposit(shares=500, symbol_id=3)    # 500 ETH
        acc1.deposit(shares=10_000, symbol_id=4) # 10,000 NVDA

        self.risk_engine.get_or_create_account(2, initial_cash=10_000_000.0, initial_shares=100_000, symbol_id=1)
        self.risk_engine.get_or_create_account(99, initial_cash=100_000_000.0, initial_shares=1_000_000, symbol_id=1)

        self.bot_active = True
        self.bot_speed = "medium"
        self.bot_delay = 0.04  # ~25 ord/s
        self._bot_task: Optional[asyncio.Task] = None
        self.connected_clients: List[WebSocket] = []

        # Real-Market Data Replayer & ML Pattern Impact Engine
        self.replayer = HistoricalMarketReplayer(router=self.router, default_ticker="NVDA")
        for sym in ("NVDA", "NVDA-USD", "AAPL", "AAPL-USD", "MSFT", "MSFT-USD", "TSLA", "TSLA-USD", "RELIANCE", "RELIANCE-BSE", "BTC-USD", "ETH-USD", "SOL-USD"):
            try:
                self.replayer.bind_stock(sym)
            except Exception:
                pass

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
        venue.total_orders += 1

        # For Market sweeps, set aggressive price crossing the book
        if type_str.upper() == "MARKET":
            exec_price = to_fixed_price(venue.info.initial_price * 1.5 if side == OrderSide.BUY else venue.info.initial_price * 0.5)
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
            buy_c = self.router.client_order_registry.get(m["buy_order_id"], 1)
            sell_c = self.router.client_order_registry.get(m["sell_order_id"], 2)
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
        snap["candles"] = self.replayer.get_candlesticks(venue.info.ticker, limit=60)
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


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Auto-start background living market simulation loop on server startup."""
    if server.bot_active:
        if server._bot_task is None or server._bot_task.done():
            server._bot_task = asyncio.create_task(_bot_simulation_loop())
    yield


# Initialize App & Server instance
server = WebTradingServer()
app = FastAPI(title="ChronosMatch Multi-Asset Trading Terminal", lifespan=lifespan)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Return empty 204 No Content for favicon requests to prevent 404 logs."""
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve single-page trading terminal."""
    index_path = Path(__file__).parent / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return index_path.read_text(encoding="utf-8")


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


@app.get("/api/snapshot")
async def api_snapshot(symbol: Optional[str] = None):
    """Fetch current market snapshot for active or specified symbol."""
    return server.get_snapshot(symbol=symbol)


@app.get("/api/symbols")
async def api_symbols():
    """Retrieve metadata for all tradeable financial instruments."""
    return server.router.list_symbols()


@app.get("/api/tickers")
async def api_tickers():
    """Retrieve 24h market metrics across all symbols."""
    return server.router.get_all_tickers()


@app.post("/api/symbol/select")
async def api_select_symbol(req: SymbolSelectRequest):
    """Switch active market symbol."""
    try:
        venue = server.router.get_venue(req.symbol)
    except KeyError:
        venue = server.replayer.bind_stock(req.symbol)
    server.active_symbol = venue.info.ticker
    return {"active_symbol": server.active_symbol}


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
    if server.bot_active:
        if server._bot_task is None or server._bot_task.done():
            server._bot_task = asyncio.create_task(_bot_simulation_loop())
    return {"bot_active": server.bot_active, "speed": server.bot_speed}


async def _bot_simulation_loop():
    """Generates continuous realistic background order flow across all active venues."""
    while server.bot_active:
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
        except Exception:
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


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """High-frequency WebSocket broadcasting live snapshots for the active symbol at ~16 FPS."""
    await ws.accept()
    if server.bot_active and (server._bot_task is None or server._bot_task.done()):
        server._bot_task = asyncio.create_task(_bot_simulation_loop())
    server.connected_clients.append(ws)
    try:
        while True:
            snapshot = server.get_snapshot()
            await ws.send_json(snapshot)
            await asyncio.sleep(0.06)  # 60 ms refresh (~16 FPS)
    except WebSocketDisconnect:
        if ws in server.connected_clients:
            server.connected_clients.remove(ws)
    except Exception:
        if ws in server.connected_clients:
            server.connected_clients.remove(ws)
