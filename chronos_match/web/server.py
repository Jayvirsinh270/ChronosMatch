"""
ChronosMatch: Interactive Web GUI Server.
FastAPI + WebSocket backend providing real-time Level 2 market depth,
live order placement, trade ticker streaming, and automated liquidity simulation.
"""

import asyncio
import os
from pathlib import Path
import random
import time
from typing import Dict, List, Optional, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
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
from chronos_match.monitor.metrics import LatencyProfiler
from chronos_match.monitor.whale_detector import WhaleDetector
from chronos_match.market.distribution import OrderDistributionGenerator
from chronos_match.ledger.audit_logger import AuditLedger


class OrderRequest(BaseModel):
    side: str               # "BUY" or "SELL"
    order_type: str = "LIMIT"  # "LIMIT" or "MARKET"
    price: float = 150.00
    quantity: int = 100
    # Phase 1: Advanced Order Type fields
    tif: str = "GTC"        # "GTC" | "IOC" | "FOK"
    post_only: bool = False  # Maker-only: reject if would cross spread
    stp: str = "CANCEL_AGGRESSOR"  # "ALLOW" | "CANCEL_AGGRESSOR" | "CANCEL_RESTING" | "CANCEL_BOTH"


class CancelRequest(BaseModel):
    order_id: int


class BotSpeedRequest(BaseModel):
    speed: str  # "slow", "medium", "fast", "hft"


class WebTradingServer:
    def __init__(self, db_path: str = "chronos_audit.db"):
        self.book = FastOrderBook(arena_capacity=100_000)
        self.profiler = LatencyProfiler(window_size=50_000)
        self.whale_detector = WhaleDetector()
        self.ledger = AuditLedger(db_path=db_path)
        self.generator = OrderDistributionGenerator(initial_price=150.00)

        # STP client registry: order_id -> client_id
        self._client_order_registry: Dict[int, int] = {}
        self.policy_engine = OrderPolicyEngine(
            book=self.book,
            client_order_registry=self._client_order_registry,
        )

        # Pre-Trade Risk Gatekeeper
        self.risk_engine = PreTradeRiskEngine()
        self.risk_engine.get_or_create_account(1, initial_cash=10_000_000.0, initial_shares=100_000)
        self.risk_engine.get_or_create_account(2, initial_cash=10_000_000.0, initial_shares=100_000)
        self.risk_engine.get_or_create_account(99, initial_cash=100_000_000.0, initial_shares=1_000_000)

        self.order_id_counter = 1
        self.total_orders = 0
        self.total_trades = 0
        self.total_volume = 0
        self.recent_trades: List[Dict[str, Any]] = []

        self.bot_active = False
        self.bot_speed = "medium"
        self.bot_delay = 0.04  # ~25 ord/s
        self._bot_task: Optional[asyncio.Task] = None
        self.connected_clients: List[WebSocket] = []

        # User tracking & quantitative market metrics
        self.user_open_orders: Dict[int, Dict[str, Any]] = {}
        self.price_history: List[Dict[str, Any]] = []
        self.last_trade_price = 150.00
        self.high_price = 150.10
        self.low_price = 149.90
        self.cumulative_notional = 0.0

        # Pre-seed realistic two-sided liquidity on startup
        self.seed_initial_liquidity()

    def seed_initial_liquidity(self):
        """Pre-populate the book with 8 bids and 8 asks around $150.00."""
        base_bid = 150.00
        base_ask = 150.05

        # 8 Bids (client_id=99 so they don't STP against user client_id=1)
        for i in range(8):
            p = base_bid - (i * 0.05)
            oid = self.order_id_counter
            self.book.add_limit_order(
                order_id=oid,
                client_id=99,
                price=to_fixed_price(p),
                quantity=random.choice([200, 500, 1000, 1500]),
                side=int(OrderSide.BUY),
                timestamp_ns=time.perf_counter_ns(),
            )
            self._client_order_registry[oid] = 99
            self.order_id_counter += 1
            self.total_orders += 1

        # 8 Asks (client_id=99)
        for i in range(8):
            p = base_ask + (i * 0.05)
            oid = self.order_id_counter
            self.book.add_limit_order(
                order_id=oid,
                client_id=99,
                price=to_fixed_price(p),
                quantity=random.choice([200, 500, 1000, 1500]),
                side=int(OrderSide.SELL),
                timestamp_ns=time.perf_counter_ns(),
            )
            self._client_order_registry[oid] = 99
            self.order_id_counter += 1
            self.total_orders += 1

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
    ) -> Dict[str, Any]:
        """Execute an order through the policy engine and into the pure-C matching core."""
        side = OrderSide.BUY if side_str.upper() == "BUY" else OrderSide.SELL
        order_id = self.order_id_counter
        self.order_id_counter += 1
        self.total_orders += 1

        # For Market sweeps, set aggressive price crossing the book
        if type_str.upper() == "MARKET":
            exec_price = to_fixed_price(200.0 if side == OrderSide.BUY else 50.0)
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
        best_bid_p = from_fixed_price(self.book.best_bid_price)
        best_ask_p = from_fixed_price(self.book.best_ask_price)
        risk_decision = self.risk_engine.validate_order(
            order_id=order_id,
            client_id=client_id,
            side=int(side),
            price_float=price_float,
            quantity=quantity,
            symbol_id=1,
            best_bid_float=best_bid_p,
            best_ask_float=best_ask_p,
            is_market=(type_str.upper() == "MARKET"),
            timestamp_ns=t0_ns,
        )

        if not risk_decision.passed:
            t1_ns = time.perf_counter_ns()
            lat_ns = t1_ns - t0_ns
            self.profiler.record_delta(t0_ns, t1_ns)
            return {
                "success": False,
                "order_id": order_id,
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

        policy_result = self.policy_engine.apply(
            order_id=order_id,
            client_id=client_id,
            price_fixed=exec_price,
            quantity=quantity,
            side=int(side),
            symbol_id=1,
            timestamp_ns=t0_ns,
            tif=tif,
            post_only=post_only,
            stp_action=stp_action,
        )

        t1_ns = time.perf_counter_ns()
        lat_ns = t1_ns - t0_ns
        self.profiler.record_delta(t0_ns, t1_ns)

        # If rejected by policy (FOK/POST_ONLY), return early with rejection info
        if not policy_result.accepted:
            return {
                "success": False,
                "order_id": order_id,
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
            self.total_trades += 1
            self.total_volume += qty
            trade_p = from_fixed_price(m["execution_price"])
            self.last_trade_price = trade_p
            self.high_price = max(self.high_price, trade_p)
            self.low_price = min(self.low_price, trade_p)
            self.cumulative_notional += trade_p * qty

            tp = TradePacket(
                trade_id=m["trade_id"],
                buy_order_id=m["buy_order_id"],
                sell_order_id=m["sell_order_id"],
                price=m["execution_price"],
                quantity=qty,
                symbol_id=1,
                aggressor_side=m["aggressor_side"],
                match_timestamp_ns=t1_ns,
                latency_ns=lat_ns,
            )
            self.whale_detector.inspect_trade(tp)
            self.ledger.insert_trade(tp)

            trade_data = {
                "trade_id": tp.trade_id,
                "side": "BUY" if m["aggressor_side"] == int(OrderSide.BUY) else "SELL",
                "price": trade_p,
                "quantity": qty,
                "latency_us": lat_ns / 1000.0,
                "time": time.strftime("%H:%M:%S"),
            }
            matched_info.append(trade_data)
            self.recent_trades.insert(0, trade_data)

            # Settle trade in Risk Engine
            buy_c = self._client_order_registry.get(m["buy_order_id"], 1)
            sell_c = self._client_order_registry.get(m["sell_order_id"], 2)
            self.risk_engine.on_trade_executed(
                buy_client_id=buy_c,
                sell_client_id=sell_c,
                price_float=trade_p,
                quantity=qty,
                symbol_id=1,
                buy_order_id=m["buy_order_id"],
                sell_order_id=m["sell_order_id"],
            )

        # Record price tick for live chart
        if matches:
            self.price_history.append({
                "time": time.strftime("%H:%M:%S"),
                "price": self.last_trade_price,
                "volume": filled_shares,
            })
            if len(self.price_history) > 100:
                self.price_history = self.price_history[-100:]

        if len(self.recent_trades) > 30:
            self.recent_trades = self.recent_trades[:30]

        # Track user resting orders for cancellation
        # (GTC/POST_ONLY limits that didn't fully fill rest on the book)
        if client_id == 1 and type_str.upper() == "LIMIT" and tif == TimeInForce.GTC:
            rem = quantity - filled_shares
            if rem > 0:
                self.risk_engine.on_order_accepted(
                    order_id=order_id,
                    client_id=client_id,
                    side=int(side),
                    price_float=price_float,
                    quantity=rem,
                    symbol_id=1,
                )
                self.user_open_orders[order_id] = {
                    "order_id": order_id,
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

    def cancel_user_order(self, order_id: int) -> Dict[str, Any]:
        """Instant O(1) order cancellation by order_id."""
        t0 = time.perf_counter_ns()
        cancelled = self.book.cancel_order(order_id)
        t1 = time.perf_counter_ns()
        lat_ns = t1 - t0
        self.profiler.record_delta(t0, t1)

        if order_id in self.user_open_orders:
            del self.user_open_orders[order_id]

        self.risk_engine.on_order_cancelled(order_id=order_id, client_id=1)

        return {
            "success": cancelled,
            "order_id": order_id,
            "latency_us": lat_ns / 1000.0,
        }

    def get_snapshot(self) -> Dict[str, Any]:
        """Construct real-time market snapshot for frontend consumption."""
        raw_l2 = self.book.get_l2_snapshot(depth=10)
        stats = self.profiler.compute_stats()

        # Format L2 prices to float
        bids = [
            {"price": from_fixed_price(b["price"]), "volume": b["volume"], "order_count": b["order_count"]}
            for b in raw_l2["bids"]
        ]
        asks = [
            {"price": from_fixed_price(a["price"]), "volume": a["volume"], "order_count": a["order_count"]}
            for a in raw_l2["asks"]
        ]

        best_bid = from_fixed_price(self.book.best_bid_price)
        best_ask = from_fixed_price(self.book.best_ask_price)
        spread = from_fixed_price(self.book.spread)

        total_bid_vol = sum(b["volume"] for b in bids)
        total_ask_vol = sum(a["volume"] for a in asks)
        denom = total_bid_vol + total_ask_vol
        imbalance = ((total_bid_vol - total_ask_vol) / denom) if denom > 0 else 0.0
        vwap = (self.cumulative_notional / max(1, self.total_volume)) if self.total_volume > 0 else self.last_trade_price

        recent_whales = [
            a.to_dict() for a in self.whale_detector.get_recent_alerts(limit=5)
        ]

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "last_price": self.last_trade_price,
            "high_24h": round(self.high_price, 2),
            "low_24h": round(self.low_price, 2),
            "vwap": round(vwap, 2),
            "imbalance": round(imbalance, 3),
            "total_bid_vol": total_bid_vol,
            "total_ask_vol": total_ask_vol,
            "l2": {"bids": bids, "asks": asks},
            "recent_trades": self.recent_trades[:20],
            "price_history": self.price_history[-50:],
            "open_orders": list(self.user_open_orders.values()),
            "whale_alerts": recent_whales,
            "total_orders": self.total_orders,
            "total_trades": self.total_trades,
            "total_volume": self.total_volume,
            "orders_per_sec": int(1.0 / max(0.001, self.bot_delay)) if self.bot_active else 0,
            "stats": stats._asdict() if stats else None,
            "bot_active": self.bot_active,
            "bot_speed": self.bot_speed,
            "risk": self.risk_engine.get_risk_metrics(),
            "account": self.risk_engine.get_or_create_account(1).to_dict(),
        }


# Initialize App & Server instance
app = FastAPI(title="ChronosMatch Web Trading Terminal")
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
    """Place a new interactive Buy/Sell order with TIF, Post-Only, and STP modifiers."""
    res = server.place_order(
        side_str=req.side,
        type_str=req.order_type,
        price_float=req.price,
        quantity=req.quantity,
        tif_str=req.tif,
        post_only=req.post_only,
        stp_str=req.stp,
    )
    return res


@app.get("/api/snapshot")
async def api_snapshot():
    """Fetch current market snapshot."""
    return server.get_snapshot()


@app.post("/api/cancel")
async def api_cancel(req: CancelRequest):
    """Cancel a resting order by order_id."""
    return server.cancel_user_order(req.order_id)


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
    """Toggle background liquidity simulation."""
    server.bot_active = not server.bot_active
    if server.bot_active:
        if server._bot_task is None or server._bot_task.done():
            server._bot_task = asyncio.create_task(_bot_simulation_loop())
    return {"bot_active": server.bot_active, "speed": server.bot_speed}


async def _bot_simulation_loop():
    """Generates continuous realistic background order flow when bot is ON."""
    while server.bot_active:
        try:
            # Generate a realistic random order
            pkt = server.generator.generate_next_order()
            side_str = "BUY" if pkt.side == OrderSide.BUY else "SELL"
            type_str = "CANCEL" if pkt.order_type == OrderType.CANCEL else "LIMIT"

            if type_str == "CANCEL":
                server.book.cancel_order(pkt.order_id)
            else:
                server.place_order(
                    side_str=side_str,
                    type_str=type_str,
                    price_float=from_fixed_price(pkt.price),
                    quantity=pkt.quantity,
                    client_id=pkt.client_id,
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
    """High-frequency WebSocket broadcasting live snapshots at ~16 FPS."""
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
