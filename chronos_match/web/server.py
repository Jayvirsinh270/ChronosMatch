"""
ChronosMatch: Interactive Web GUI Server & Multi-Symbol Trading Gateway.
FastAPI + WebSocket backend providing real-time Level 2 market depth,
live order placement, multi-asset matching router, risk checks, and automated simulation.
"""

import asyncio
import os
from pathlib import Path
import random
import time
from typing import Dict, List, Optional, Any, Union

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
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


class WebTradingServer:
    def __init__(self, db_path: str = "chronos_audit.db"):
        self.router = MultiBookRouter()
        self.ledger = AuditLedger(db_path=db_path)
        self.risk_engine = PreTradeRiskEngine()
        self.active_symbol = "SOL-USD"

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

        self.bot_active = False
        self.bot_speed = "medium"
        self.bot_delay = 0.04  # ~25 ord/s
        self._bot_task: Optional[asyncio.Task] = None
        self.connected_clients: List[WebSocket] = []

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

        self.risk_engine.on_order_cancelled(order_id=order_id, client_id=1)

        return {
            "success": cancelled,
            "order_id": order_id,
            "symbol": venue.info.ticker,
            "latency_us": lat_ns / 1000.0,
        }

    def get_snapshot(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Construct real-time market snapshot for the active or requested symbol."""
        venue = self.router.get_venue(symbol or self.active_symbol)
        snap = venue.get_snapshot()

        snap["orders_per_sec"] = int(1.0 / max(0.001, self.bot_delay)) if self.bot_active else 0
        snap["bot_active"] = self.bot_active
        snap["bot_speed"] = self.bot_speed
        snap["risk"] = self.risk_engine.get_risk_metrics()
        snap["account"] = self.risk_engine.get_or_create_account(1).to_dict()
        snap["active_symbol"] = self.active_symbol
        snap["symbols"] = self.router.list_symbols()
        snap["tickers"] = self.router.get_all_tickers()
        return snap


# Initialize App & Server instance
app = FastAPI(title="ChronosMatch Multi-Asset Trading Terminal")
server = WebTradingServer()


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
    venue = server.router.get_venue(req.symbol)
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
            # Pick a random registered venue to generate realistic market ticks
            venues = list(server.router.venues_by_id.values())
            venue = random.choice(venues)
            pkt = venue.generator.generate_next_order()
            side_str = "BUY" if pkt.side == OrderSide.BUY else "SELL"
            type_str = "CANCEL" if pkt.order_type == OrderType.CANCEL else "LIMIT"

            if type_str == "CANCEL":
                venue.book.cancel_order(pkt.order_id)
            else:
                server.place_order(
                    side_str=side_str,
                    type_str=type_str,
                    price_float=from_fixed_price(pkt.price),
                    quantity=pkt.quantity,
                    client_id=pkt.client_id,
                    symbol=venue.info.ticker,
                )
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


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """High-frequency WebSocket broadcasting live snapshots for the active symbol at ~16 FPS."""
    await ws.accept()
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
