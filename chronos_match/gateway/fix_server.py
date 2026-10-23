"""
ChronosMatch: Asynchronous Institutional FIX 4.2 Gateway Server.

High-throughput TCP server dispatching inbound FIX messages into the MultiBookRouter,
PreTradeRiskEngine, and WriteAheadLog with sub-millisecond execution reports.
"""

import asyncio
import logging
import time
from typing import Dict, Optional, Tuple, Any

from chronos_match.market.router import MultiBookRouter, SingleSymbolVenue
from chronos_match.risk.risk_engine import PreTradeRiskEngine
from chronos_match.persistence.wal import WriteAheadLog
from chronos_match.engine.order_policies import TimeInForce, SelfTradeAction
from chronos_match.protocol import (
    OrderSide,
    to_fixed_price,
    from_fixed_price,
    TradePacket,
)
from chronos_match.gateway.fix_protocol import (
    FixMessage,
    FixTag,
    FixMsgType,
    FixSide,
    FixOrdType,
    FixTimeInForce,
    FixExecType,
    FixOrdStatus,
    make_execution_report,
    SOH,
)
from chronos_match.gateway.fix_session import FixSession

logger = logging.getLogger("ChronosFIX")


class FixGatewayServer:
    """
    Institutional TCP FIX 4.2 Gateway Server.
    """

    def __init__(
        self,
        router: MultiBookRouter,
        risk_engine: PreTradeRiskEngine,
        wal: Optional[WriteAheadLog] = None,
        host: str = "127.0.0.1",
        port: int = 9800,
        exchange_comp_id: str = "CHRONOS",
    ):
        self.router = router
        self.risk_engine = risk_engine
        self.wal = wal
        self.host = host
        self.port = port
        self.exchange_comp_id = exchange_comp_id
        self._server: Optional[asyncio.Server] = None
        self.active_sessions: Dict[str, FixSession] = {}
        self.exec_id_counter = 1000

    def next_exec_id(self) -> str:
        self.exec_id_counter += 1
        return f"EXEC_{self.exec_id_counter}"

    async def start(self) -> None:
        """Start accepting incoming TCP FIX connections."""
        self._server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port,
        )

    async def stop(self) -> None:
        """Stop TCP FIX Gateway server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle individual client TCP stream."""
        session = FixSession(target_comp_id=self.exchange_comp_id)
        buffer = bytearray()

        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break

                buffer.extend(chunk)

                # Extract complete FIX messages (ended by 10=xxx\x01)
                while True:
                    idx = buffer.find(b"10=")
                    if idx == -1:
                        break
                    # Find trailing SOH or newline after checksum
                    soh_idx = buffer.find(SOH, idx)
                    if soh_idx == -1:
                        # Message partially received, wait for rest
                        break

                    msg_bytes = bytes(buffer[:soh_idx + 1])
                    del buffer[:soh_idx + 1]

                    try:
                        fix_msg = FixMessage.parse(msg_bytes)
                        responses = await self.process_message(fix_msg, session)
                        for resp in responses:
                            writer.write(session.format_outgoing(resp))
                        await writer.drain()
                    except Exception as ex:
                        logger.error(f"Error processing FIX message: {ex}")

        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def process_message(self, msg: FixMessage, session: FixSession) -> list[FixMessage]:
        """Process inbound FIX message and return outgoing response messages."""
        msg_type = msg.msg_type

        # Admin messages handled by session state machine
        if msg_type in (FixMsgType.LOGON, FixMsgType.LOGOUT, FixMsgType.HEARTBEAT, FixMsgType.TEST_REQUEST):
            resps = session.handle_incoming(msg)
            if session.sender_comp_id:
                self.active_sessions[session.sender_comp_id] = session
            return resps

        # Session must be authenticated before sending application orders
        if not session.authenticated:
            rej = FixMessage(FixMsgType.REJECT)
            rej.set(FixTag.Text, "Session not authenticated. Send 35=A Logon first.")
            return [rej]

        # Application Messages
        if msg_type == FixMsgType.NEW_ORDER_SINGLE:
            return self._handle_new_order_single(msg, session)
        elif msg_type == FixMsgType.ORDER_CANCEL_REQUEST:
            return self._handle_order_cancel_request(msg, session)

        return []

    def _handle_new_order_single(self, msg: FixMessage, session: FixSession) -> list[FixMessage]:
        """Execute FIX NewOrderSingle (35=D)."""
        cl_ord_id = msg.get(FixTag.ClOrdID, "")
        symbol = msg.get(FixTag.Symbol, "SOL-USD")
        side_fix = msg.get(FixTag.Side, FixSide.BUY)
        qty = msg.get_int(FixTag.OrderQty, 100)
        price_float = msg.get_float(FixTag.Price, 150.00)
        ord_type_fix = msg.get(FixTag.OrdType, FixOrdType.LIMIT)
        tif_fix = msg.get(FixTag.TimeInForce, FixTimeInForce.GTC)

        side = OrderSide.BUY if side_fix == FixSide.BUY else OrderSide.SELL

        # Map FIX TimeInForce
        if tif_fix == FixTimeInForce.IOC:
            tif = TimeInForce.IOC
        elif tif_fix == FixTimeInForce.FOK:
            tif = TimeInForce.FOK
        else:
            tif = TimeInForce.GTC

        is_market = (ord_type_fix == FixOrdType.MARKET)
        if is_market:
            tif = TimeInForce.IOC

        try:
            venue = self.router.get_venue(symbol)
        except KeyError:
            return [make_execution_report(
                order_id=0, cl_ord_id=cl_ord_id, exec_id=self.next_exec_id(),
                exec_type=FixExecType.REJECTED, ord_status=FixOrdStatus.REJECTED,
                symbol=symbol, side=side_fix, order_qty=qty, price=price_float,
                leaves_qty=0, cum_qty=0, reject_reason=f"Unknown Symbol: {symbol}",
            )]

        client_id = 1
        order_id = self.router.allocate_order_id()
        session.cl_ord_to_order_id[cl_ord_id] = order_id
        session.order_id_to_cl_ord[order_id] = cl_ord_id
        self.router.client_order_registry[order_id] = client_id

        # Pre-Trade Risk Gatekeeping Check
        t0_ns = time.perf_counter_ns()
        best_bid_p = from_fixed_price(venue.book.best_bid_price)
        best_ask_p = from_fixed_price(venue.book.best_ask_price)

        risk_decision = self.risk_engine.validate_order(
            order_id=order_id,
            client_id=client_id,
            side=int(side),
            price_float=price_float,
            quantity=qty,
            symbol_id=venue.info.symbol_id,
            best_bid_float=best_bid_p,
            best_ask_float=best_ask_p,
            is_market=is_market,
            timestamp_ns=t0_ns,
        )

        if not risk_decision.passed:
            return [make_execution_report(
                order_id=order_id, cl_ord_id=cl_ord_id, exec_id=self.next_exec_id(),
                exec_type=FixExecType.REJECTED, ord_status=FixOrdStatus.REJECTED,
                symbol=symbol, side=side_fix, order_qty=qty, price=price_float,
                leaves_qty=0, cum_qty=0, reject_reason=risk_decision.reject_reason,
            )]

        price_fixed = to_fixed_price(price_float)

        # Durable WAL journal before matching
        if self.wal:
            self.wal.append_order_submit(
                order_id=order_id,
                client_id=client_id,
                symbol_id=venue.info.symbol_id,
                side=int(side),
                price=price_fixed,
                quantity=qty,
                tif=int(tif),
                post_only=0,
                stp_action=int(SelfTradeAction.ALLOW),
                timestamp_ns=t0_ns,
            )

        # Policy & Order Book Match
        policy_res = venue.policy_engine.apply(
            order_id=order_id,
            client_id=client_id,
            price_fixed=price_fixed,
            quantity=qty,
            side=int(side),
            symbol_id=venue.info.symbol_id,
            timestamp_ns=t0_ns,
            tif=tif,
            post_only=False,
            stp_action=SelfTradeAction.ALLOW,
        )

        if not policy_res.accepted:
            return [make_execution_report(
                order_id=order_id, cl_ord_id=cl_ord_id, exec_id=self.next_exec_id(),
                exec_type=FixExecType.REJECTED, ord_status=FixOrdStatus.REJECTED,
                symbol=symbol, side=side_fix, order_qty=qty, price=price_float,
                leaves_qty=0, cum_qty=0, reject_reason=policy_res.reject_reason,
            )]

        reports: list[FixMessage] = []
        filled = policy_res.total_filled
        cum_qty = 0
        total_val = 0.0

        # Immediate Fills
        for m in policy_res.matches:
            match_q = m["quantity"]
            match_p = from_fixed_price(m["execution_price"])
            cum_qty += match_q
            total_val += match_p * match_q
            avg_p = total_val / cum_qty
            leaves_q = qty - cum_qty

            venue.total_trades += 1
            venue.total_volume += match_q
            venue.last_trade_price = match_p

            exec_status = FixOrdStatus.FILLED if leaves_q == 0 else FixOrdStatus.PARTIALLY_FILLED
            exec_type = FixExecType.FILL if leaves_q == 0 else FixExecType.PARTIAL_FILL

            reports.append(make_execution_report(
                order_id=order_id,
                cl_ord_id=cl_ord_id,
                exec_id=self.next_exec_id(),
                exec_type=exec_type,
                ord_status=exec_status,
                symbol=symbol,
                side=side_fix,
                order_qty=qty,
                price=price_float,
                leaves_qty=leaves_q,
                cum_qty=cum_qty,
                avg_px=avg_p,
                last_px=match_p,
                last_shares=match_q,
            ))

        # If resting remainder exists, emit New acknowledgment
        if filled < qty and tif == TimeInForce.GTC:
            leaves_q = qty - filled
            venue.user_open_orders[order_id] = {
                "order_id": order_id,
                "symbol": symbol,
                "side": "BUY" if side == OrderSide.BUY else "SELL",
                "price": price_float,
                "quantity": qty,
                "filled": filled,
                "remaining": leaves_q,
                "tif": tif.name,
                "post_only": False,
                "time": time.strftime("%H:%M:%S"),
            }
            if filled == 0:
                reports.append(make_execution_report(
                    order_id=order_id,
                    cl_ord_id=cl_ord_id,
                    exec_id=self.next_exec_id(),
                    exec_type=FixExecType.NEW,
                    ord_status=FixOrdStatus.NEW,
                    symbol=symbol,
                    side=side_fix,
                    order_qty=qty,
                    price=price_float,
                    leaves_qty=leaves_q,
                    cum_qty=0,
                ))

        return reports

    def _handle_order_cancel_request(self, msg: FixMessage, session: FixSession) -> list[FixMessage]:
        """Execute FIX OrderCancelRequest (35=F)."""
        cl_ord_id = msg.get(FixTag.ClOrdID, "")
        orig_cl_ord_id = msg.get(FixTag.OrigClOrdID, "")
        symbol = msg.get(FixTag.Symbol, "SOL-USD")

        order_id = session.cl_ord_to_order_id.get(orig_cl_ord_id)
        if order_id is None:
            return [session.make_order_cancel_reject(cl_ord_id, orig_cl_ord_id, "OrigClOrdID not found")]

        try:
            venue = self.router.get_venue(symbol)
        except KeyError:
            return [session.make_order_cancel_reject(cl_ord_id, orig_cl_ord_id, f"Unknown Symbol {symbol}")]

        cancelled = venue.book.cancel_order(order_id)
        if not cancelled:
            return [session.make_order_cancel_reject(cl_ord_id, orig_cl_ord_id, "Order already filled or cancelled")]

        if self.wal:
            self.wal.append_order_cancel(
                order_id=order_id,
                client_id=1,
                symbol_id=venue.info.symbol_id,
                timestamp_ns=time.perf_counter_ns(),
            )

        venue.user_open_orders.pop(order_id, None)
        self.risk_engine.on_order_cancelled(order_id=order_id, client_id=1)

        # Emit ExecutionReport (Cancelled)
        return [make_execution_report(
            order_id=order_id,
            cl_ord_id=cl_ord_id,
            exec_id=self.next_exec_id(),
            exec_type=FixExecType.CANCELLED,
            ord_status=FixOrdStatus.CANCELLED,
            symbol=symbol,
            side=FixSide.BUY,
            order_qty=0,
            price=0.0,
            leaves_qty=0,
            cum_qty=0,
        )]
