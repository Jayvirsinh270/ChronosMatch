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
from chronos_match.monitor.metrics import LatencyProfiler
from chronos_match.monitor.whale_detector import WhaleDetector
from chronos_match.market.distribution import OrderDistributionGenerator
from chronos_match.ledger.audit_logger import AuditLedger


class OrderRequest(BaseModel):
    side: str            # "BUY" or "SELL"
    type: str = "LIMIT"  # "LIMIT" or "MARKET"
    price: float = 150.00
    quantity: int = 100


class WebTradingServer:
    def __init__(self, db_path: str = "chronos_audit.db"):
        self.book = FastOrderBook(arena_capacity=100_000)
        self.profiler = LatencyProfiler(window_size=50_000)
        self.whale_detector = WhaleDetector()
        self.ledger = AuditLedger(db_path=db_path)
        self.generator = OrderDistributionGenerator(initial_price=150.00)

        self.order_id_counter = 1
        self.total_orders = 0
        self.total_trades = 0
        self.total_volume = 0
        self.recent_trades: List[Dict[str, Any]] = []

        self.bot_active = False
        self._bot_task: Optional[asyncio.Task] = None
        self.connected_clients: List[WebSocket] = []

        # Pre-seed realistic two-sided liquidity on startup
        self.seed_initial_liquidity()

    def seed_initial_liquidity(self):
        """Pre-populate the book with 8 bids and 8 asks around $150.00."""
        base_bid = 150.00
        base_ask = 150.05

        # 8 Bids
        for i in range(8):
            p = base_bid - (i * 0.05)
            self.book.add_limit_order(
                order_id=self.order_id_counter,
                client_id=1,
                price=to_fixed_price(p),
                quantity=random.choice([200, 500, 1000, 1500]),
                side=int(OrderSide.BUY),
                timestamp_ns=time.perf_counter_ns(),
            )
            self.order_id_counter += 1
            self.total_orders += 1

        # 8 Asks
        for i in range(8):
            p = base_ask + (i * 0.05)
            self.book.add_limit_order(
                order_id=self.order_id_counter,
                client_id=2,
                price=to_fixed_price(p),
                quantity=random.choice([200, 500, 1000, 1500]),
                side=int(OrderSide.SELL),
                timestamp_ns=time.perf_counter_ns(),
            )
            self.order_id_counter += 1
            self.total_orders += 1

    def place_order(
        self,
        side_str: str,
        type_str: str,
        price_float: float,
        quantity: int,
        client_id: int = 1,
    ) -> Dict[str, Any]:
        """Execute an order directly inside pure-C Cython matching engine."""
        side = OrderSide.BUY if side_str.upper() == "BUY" else OrderSide.SELL
        order_id = self.order_id_counter
        self.order_id_counter += 1
        self.total_orders += 1

        # For Market sweeps, set aggressive price crossing the book
        if type_str.upper() == "MARKET":
            exec_price = to_fixed_price(200.0 if side == OrderSide.BUY else 50.0)
        else:
            exec_price = to_fixed_price(price_float)

        t0_ns = time.perf_counter_ns()
        matches = self.book.add_limit_order(
            order_id=order_id,
            client_id=client_id,
            price=exec_price,
            quantity=quantity,
            side=int(side),
            symbol_id=1,
            timestamp_ns=t0_ns,
        )
        t1_ns = time.perf_counter_ns()
        lat_ns = t1_ns - t0_ns
        self.profiler.record_delta(t0_ns, t1_ns)

        filled_shares = 0
        matched_info = []

        if matches:
            for m in matches:
                qty = m["quantity"]
                filled_shares += qty
                self.total_trades += 1
                self.total_volume += qty

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
                    "price": from_fixed_price(m["execution_price"]),
                    "quantity": qty,
                    "latency_us": lat_ns / 1000.0,
                    "time": time.strftime("%H:%M:%S"),
                }
                matched_info.append(trade_data)
                self.recent_trades.insert(0, trade_data)

            if len(self.recent_trades) > 30:
                self.recent_trades = self.recent_trades[:30]

        return {
            "success": True,
            "order_id": order_id,
            "side": side_str.upper(),
            "type": type_str.upper(),
            "price": price_float,
            "quantity": quantity,
            "total_filled": filled_shares,
            "matched_trades": matched_info,
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

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "l2": {"bids": bids, "asks": asks},
            "recent_trades": self.recent_trades[:15],
            "total_orders": self.total_orders,
            "total_trades": self.total_trades,
            "total_volume": self.total_volume,
            "orders_per_sec": 50 if self.bot_active else 0,
            "stats": stats._asdict() if stats else None,
            "bot_active": self.bot_active,
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
    """Place a new interactive Buy/Sell order."""
    res = server.place_order(
        side_str=req.side,
        type_str=req.type,
        price_float=req.price,
        quantity=req.quantity,
    )
    return res


@app.get("/api/snapshot")
async def api_snapshot():
    """Fetch current market snapshot."""
    return server.get_snapshot()


@app.post("/api/bot/toggle")
async def api_toggle_bot():
    """Toggle background liquidity simulation."""
    server.bot_active = not server.bot_active
    if server.bot_active:
        if server._bot_task is None or server._bot_task.done():
            server._bot_task = asyncio.create_task(_bot_simulation_loop())
    return {"bot_active": server.bot_active}


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
            await asyncio.sleep(0.02)  # ~50 orders per second
        except Exception:
            await asyncio.sleep(0.05)


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
