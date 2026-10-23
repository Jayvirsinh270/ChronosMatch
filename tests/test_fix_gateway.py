"""
ChronosMatch: Test Suite for Phase 5 (Institutional FIX 4.2 & Binary OUCH Gateway).

Validates:
1. FIX 4.2 Message serialization, framing, and 3-digit CheckSum calculation.
2. Inbound FIX parsing and checksum verification.
3. FIX Session state machine: Logon, Heartbeat, TestRequest, and Logout.
4. Unauthenticated access prevention and rejection.
5. NewOrderSingle (35=D) limit order placement and ExecutionReport (New).
6. Aggressive order matching and ExecutionReport (Fill / PartialFill).
7. OrderCancelRequest (35=F) and cancellation ExecutionReport / OrderCancelReject.
8. Pre-trade risk rejection through standard FIX ExecutionReport (Rejected).
9. Binary OUCH/ITCH codec roundtrip and serialization performance benchmark.
10. Live Asynchronous TCP FIX Gateway server network roundtrip.
"""

import asyncio
import time
import pytest

from chronos_match.market.router import MultiBookRouter, SymbolInfo
from chronos_match.risk.risk_engine import PreTradeRiskEngine, RiskLimits
from chronos_match.protocol import to_fixed_price, from_fixed_price, OrderSide
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
    SOH_CHR,
)
from chronos_match.gateway.fix_session import FixSession
from chronos_match.gateway.fix_server import FixGatewayServer
from chronos_match.gateway.binary_ouch import (
    BinaryOuchCodec,
    OuchEnterOrder,
    OuchCancelOrder,
)


# ─────────────────────────────────────────────────────────────
# 1. FIX PROTOCOL PARSING & FRAMING TESTS
# ─────────────────────────────────────────────────────────────

def test_fix_message_encode_and_checksum():
    """Verify standard FIX 4.2 wire framing, BodyLength, and 3-digit CheckSum."""
    msg = FixMessage(FixMsgType.LOGON)
    msg.set(FixTag.SenderCompID, "ALGO_BOT_1")
    msg.set(FixTag.TargetCompID, "CHRONOS")
    msg.set(FixTag.MsgSeqNum, 1)
    msg.set(FixTag.HeartBtInt, 30)

    encoded = msg.encode()
    assert encoded.startswith(b"8=FIX.4.2\x019=")
    assert b"\x0135=A\x01" in encoded
    assert b"\x0149=ALGO_BOT_1\x01" in encoded
    assert b"\x0156=CHRONOS\x01" in encoded
    assert b"\x0110=" in encoded
    assert encoded.endswith(SOH)

    # Verify checksum algorithm
    idx = encoded.rfind(b"10=")
    calc_sum = FixMessage.calculate_checksum(encoded[:idx])
    assert encoded[idx + 3:idx + 6].decode("ascii") == calc_sum


def test_fix_message_parse_and_checksum_verification():
    """Parse valid FIX raw byte sequence and detect checksum corruption."""
    raw = f"8=FIX.4.2{SOH_CHR}9=49{SOH_CHR}35=A{SOH_CHR}49=CLIENT{SOH_CHR}56=CHRONOS{SOH_CHR}34=1{SOH_CHR}52=20261023-12:00:00.000{SOH_CHR}108=30{SOH_CHR}"
    raw_b = raw.encode("ascii")
    cs = FixMessage.calculate_checksum(raw_b)
    valid_fix = raw_b + f"10={cs}{SOH_CHR}".encode("ascii")

    parsed = FixMessage.parse(valid_fix)
    assert parsed.msg_type == FixMsgType.LOGON
    assert parsed.get(FixTag.SenderCompID) == "CLIENT"
    assert parsed.get(FixTag.TargetCompID) == "CHRONOS"
    assert parsed.get_int(FixTag.HeartBtInt) == 30

    # Corrupt checksum
    corrupt_fix = raw_b + b"10=999\x01"
    with pytest.raises(ValueError) as exc:
        FixMessage.parse(corrupt_fix)
    assert "Checksum mismatch" in str(exc.value)


# ─────────────────────────────────────────────────────────────
# 2. FIX SESSION LIFECYCLE TESTS
# ─────────────────────────────────────────────────────────────

def test_fix_session_logon_heartbeat_test_request():
    """Verify session handshake: Logon -> Ack, TestRequest -> Heartbeat."""
    session = FixSession(target_comp_id="CHRONOS")
    assert session.authenticated is False

    # 1. Client sends Logon (35=A)
    logon_in = FixMessage(FixMsgType.LOGON)
    logon_in.set(FixTag.SenderCompID, "QUANT_FUND_A")
    logon_in.set(FixTag.TargetCompID, "CHRONOS")
    logon_in.set(FixTag.MsgSeqNum, 1)
    logon_in.set(FixTag.HeartBtInt, 20)

    resps = session.handle_incoming(logon_in)
    assert len(resps) == 1
    assert resps[0].msg_type == FixMsgType.LOGON
    assert resps[0].get_int(FixTag.HeartBtInt) == 20
    assert session.authenticated is True
    assert session.sender_comp_id == "QUANT_FUND_A"

    # 2. Client sends TestRequest (35=1, 112=PING_42)
    test_req = FixMessage(FixMsgType.TEST_REQUEST)
    test_req.set(FixTag.MsgSeqNum, 2)
    test_req.set(FixTag.TestReqID, "PING_42")

    resps2 = session.handle_incoming(test_req)
    assert len(resps2) == 1
    assert resps2[0].msg_type == FixMsgType.HEARTBEAT
    assert resps2[0].get(FixTag.TestReqID) == "PING_42"

    # 3. Client sends Logout (35=5)
    logout_in = FixMessage(FixMsgType.LOGOUT)
    logout_in.set(FixTag.MsgSeqNum, 3)
    resps3 = session.handle_incoming(logout_in)
    assert len(resps3) == 1
    assert resps3[0].msg_type == FixMsgType.LOGOUT
    assert session.authenticated is False


# ─────────────────────────────────────────────────────────────
# 3. FIX ORDER EXECUTION LIFECYCLE TESTS
# ─────────────────────────────────────────────────────────────

def _make_env():
    router = MultiBookRouter()
    risk = PreTradeRiskEngine()
    router.register_symbol(SymbolInfo(1, "SOL-USD", "SOL", "USD", 0.01, 1, 150.00), seed=False)
    server = FixGatewayServer(router=router, risk_engine=risk)
    session = FixSession(target_comp_id="CHRONOS")
    return server, session, router, risk


def test_fix_unauthenticated_order_rejection():
    """Orders submitted before Logon (35=A) must be rejected."""
    async def _run():
        server, session, router, risk = _make_env()

        order_msg = FixMessage(FixMsgType.NEW_ORDER_SINGLE)
        order_msg.set(FixTag.ClOrdID, "ORD_001")
        order_msg.set(FixTag.Symbol, "SOL-USD")

        resps = await server.process_message(order_msg, session)
        assert len(resps) == 1
        assert resps[0].msg_type == FixMsgType.REJECT
        assert "not authenticated" in resps[0].get(FixTag.Text, "")

    asyncio.run(_run())


def test_fix_new_order_single_resting_limit():
    """Submit limit order resting on book; verify ExecutionReport (New)."""
    async def _run():
        server, session, router, risk = _make_env()
        session.authenticated = True
        session.sender_comp_id = "BOT_1"

        # Submit Buy Limit: 100 shares @ $148.00
        order = FixMessage(FixMsgType.NEW_ORDER_SINGLE)
        order.set(FixTag.ClOrdID, "CL_101")
        order.set(FixTag.Symbol, "SOL-USD")
        order.set(FixTag.Side, FixSide.BUY)
        order.set(FixTag.OrderQty, 100)
        order.set(FixTag.Price, "148.00")
        order.set(FixTag.OrdType, FixOrdType.LIMIT)
        order.set(FixTag.TimeInForce, FixTimeInForce.GTC)

        resps = await server.process_message(order, session)
        assert len(resps) == 1

        report = resps[0]
        assert report.msg_type == FixMsgType.EXECUTION_REPORT
        assert report.get(FixTag.ClOrdID) == "CL_101"
        assert report.get(FixTag.ExecType) == FixExecType.NEW
        assert report.get(FixTag.OrdStatus) == FixOrdStatus.NEW
        assert report.get_int(FixTag.LeavesQty) == 100
        assert report.get_int(FixTag.CumQty) == 0

        # Verify order rests on engine book
        venue = router.get_venue("SOL-USD")
        assert venue.book.best_bid_price == to_fixed_price(148.00)

    asyncio.run(_run())


def test_fix_new_order_single_immediate_fill():
    """Place resting Ask, then cross with aggressive FIX Buy; verify ExecutionReport (Fill)."""
    async def _run():
        server, session, router, risk = _make_env()
        session.authenticated = True
        session.sender_comp_id = "BOT_1"
        venue = router.get_venue("SOL-USD")

        # 1. Pre-place resting Ask at $150.00 for 100 shares
        venue.book.add_limit_order(
            order_id=50,
            client_id=99,
            price=to_fixed_price(150.00),
            quantity=100,
            side=int(OrderSide.SELL),
            symbol_id=1,
        )
        router.client_order_registry[50] = 99

        # 2. Aggressive Buy via FIX: 100 shares @ $150.00
        order = FixMessage(FixMsgType.NEW_ORDER_SINGLE)
        order.set(FixTag.ClOrdID, "CL_TAKER_1")
        order.set(FixTag.Symbol, "SOL-USD")
        order.set(FixTag.Side, FixSide.BUY)
        order.set(FixTag.OrderQty, 100)
        order.set(FixTag.Price, "150.00")
        order.set(FixTag.OrdType, FixOrdType.LIMIT)

        resps = await server.process_message(order, session)
        assert len(resps) == 1

        report = resps[0]
        assert report.msg_type == FixMsgType.EXECUTION_REPORT
        assert report.get(FixTag.ClOrdID) == "CL_TAKER_1"
        assert report.get(FixTag.ExecType) == FixExecType.FILL
        assert report.get(FixTag.OrdStatus) == FixOrdStatus.FILLED
        assert report.get_int(FixTag.LastShares) == 100
        assert report.get_float(FixTag.LastPx) == 150.00
        assert report.get_int(FixTag.LeavesQty) == 0
        assert report.get_int(FixTag.CumQty) == 100

    asyncio.run(_run())


def test_fix_order_cancel_request_and_reject():
    """Test order cancellation and reject when cancelling non-existent order."""
    async def _run():
        server, session, router, risk = _make_env()
        session.authenticated = True

        # 1. Place resting limit order
        order = FixMessage(FixMsgType.NEW_ORDER_SINGLE)
        order.set(FixTag.ClOrdID, "ORIG_101")
        order.set(FixTag.Symbol, "SOL-USD")
        order.set(FixTag.Side, FixSide.BUY)
        order.set(FixTag.OrderQty, 100)
        order.set(FixTag.Price, "145.00")
        order.set(FixTag.OrdType, FixOrdType.LIMIT)
        await server.process_message(order, session)

        # 2. Cancel order
        cancel_req = FixMessage(FixMsgType.ORDER_CANCEL_REQUEST)
        cancel_req.set(FixTag.ClOrdID, "CXL_01")
        cancel_req.set(FixTag.OrigClOrdID, "ORIG_101")
        cancel_req.set(FixTag.Symbol, "SOL-USD")

        cxl_resps = await server.process_message(cancel_req, session)
        assert len(cxl_resps) == 1
        assert cxl_resps[0].msg_type == FixMsgType.EXECUTION_REPORT
        assert cxl_resps[0].get(FixTag.ExecType) == FixExecType.CANCELLED
        assert cxl_resps[0].get(FixTag.OrdStatus) == FixOrdStatus.CANCELLED

        # 3. Attempting to cancel already cancelled order emits OrderCancelReject (35=9)
        bad_cxl = FixMessage(FixMsgType.ORDER_CANCEL_REQUEST)
        bad_cxl.set(FixTag.ClOrdID, "CXL_02")
        bad_cxl.set(FixTag.OrigClOrdID, "NON_EXISTENT")
        bad_cxl.set(FixTag.Symbol, "SOL-USD")

        rej_resps = await server.process_message(bad_cxl, session)
        assert len(rej_resps) == 1
        assert rej_resps[0].msg_type == FixMsgType.ORDER_CANCEL_REJECT
        assert rej_resps[0].get(FixTag.OrigClOrdID) == "NON_EXISTENT"

    asyncio.run(_run())


def test_fix_risk_rejection():
    """Test fat-finger collar rejection returned as standard FIX ExecutionReport (Rejected)."""
    async def _run():
        server, session, router, risk = _make_env()
        session.authenticated = True

        # Pre-seed realistic best bid / ask
        venue = router.get_venue("SOL-USD")
        venue.book.add_limit_order(1, 99, to_fixed_price(150.00), 100, int(OrderSide.BUY), 1)
        venue.book.add_limit_order(2, 99, to_fixed_price(150.10), 100, int(OrderSide.SELL), 1)

        # Submit Buy at $300.00 (+100% collar violation)
        fat_finger_order = FixMessage(FixMsgType.NEW_ORDER_SINGLE)
        fat_finger_order.set(FixTag.ClOrdID, "FAT_FINGER_1")
        fat_finger_order.set(FixTag.Symbol, "SOL-USD")
        fat_finger_order.set(FixTag.Side, FixSide.BUY)
        fat_finger_order.set(FixTag.OrderQty, 100)
        fat_finger_order.set(FixTag.Price, "300.00")
        fat_finger_order.set(FixTag.OrdType, FixOrdType.LIMIT)

        resps = await server.process_message(fat_finger_order, session)
        assert len(resps) == 1
        rep = resps[0]
        assert rep.msg_type == FixMsgType.EXECUTION_REPORT
        assert rep.get(FixTag.ExecType) == FixExecType.REJECTED
        assert rep.get(FixTag.OrdStatus) == FixOrdStatus.REJECTED
        assert "PRICE_COLLAR" in rep.get(FixTag.Text, "")

    asyncio.run(_run())


# ─────────────────────────────────────────────────────────────
# 4. BINARY OUCH/ITCH CODEC TESTS
# ─────────────────────────────────────────────────────────────

def test_binary_ouch_codec_roundtrip():
    """Verify binary OUCH packing, unpacking, and exact struct field fidelity."""
    # 1. EnterOrder (26 bytes)
    packed_enter = BinaryOuchCodec.pack_enter_order(
        token=123456789,
        side="B",
        shares=500,
        stock="SOL",
        price=1502500,
    )
    assert len(packed_enter) == 26
    assert packed_enter[0:1] == b"O"

    unpacked = BinaryOuchCodec.unpack_enter_order(packed_enter)
    assert unpacked.order_token == 123456789
    assert unpacked.side == "B"
    assert unpacked.shares == 500
    assert unpacked.stock == "SOL"
    assert unpacked.price == 1502500

    # 2. CancelOrder (13 bytes)
    packed_cancel = BinaryOuchCodec.pack_cancel_order(token=123456789, shares=250)
    assert len(packed_cancel) == 13
    assert packed_cancel[0:1] == b"X"

    unpacked_c = BinaryOuchCodec.unpack_cancel_order(packed_cancel)
    assert unpacked_c.order_token == 123456789
    assert unpacked_c.shares == 250

    # 3. ITCH OrderAccepted (42 bytes)
    packed_acc = BinaryOuchCodec.pack_order_accepted(
        token=1001, order_ref=5001, side="S", shares=100, stock="BTC", price=650000000,
    )
    assert len(packed_acc) == 42
    assert packed_acc[0:1] == b"A"

    # 4. ITCH OrderExecuted (29 bytes)
    packed_exec = BinaryOuchCodec.pack_order_executed(order_ref=5001, executed_shares=100, match_num=9901)
    assert len(packed_exec) == 29
    assert packed_exec[0:1] == b"E"


def test_binary_ouch_throughput_benchmark():
    """Benchmark raw binary struct packing throughput (> 50,000 packets/sec)."""
    count = 20_000
    t0 = time.perf_counter()
    for i in range(count):
        BinaryOuchCodec.pack_enter_order(
            token=i + 1,
            side="B",
            shares=100,
            stock="SOL",
            price=1500000,
        )
    elapsed = time.perf_counter() - t0
    rate = count / max(0.0001, elapsed)
    assert rate > 50_000, f"Expected >50k ops/sec, got {rate:.0f}"


# ─────────────────────────────────────────────────────────────
# 5. ASYNC TCP FIX GATEWAY LIVE NETWORK ROUNDTRIP
# ─────────────────────────────────────────────────────────────

def test_live_tcp_fix_gateway_roundtrip():
    """Start real TCP server on localhost port 9815, connect client socket, and execute order."""
    async def _run():
        router = MultiBookRouter()
        risk = PreTradeRiskEngine()
        router.register_symbol(SymbolInfo(1, "SOL-USD", "SOL", "USD", 0.01, 1, 150.00), seed=False)

        server = FixGatewayServer(
            router=router,
            risk_engine=risk,
            host="127.0.0.1",
            port=9815,
            exchange_comp_id="CHRONOS",
        )
        await server.start()

        try:
            # Open live TCP stream to FIX Gateway
            reader, writer = await asyncio.open_connection("127.0.0.1", 9815)

            # 1. Send Logon
            logon_msg = FixMessage(FixMsgType.LOGON)
            logon_msg.set(FixTag.SenderCompID, "TCP_ALGO_1")
            logon_msg.set(FixTag.TargetCompID, "CHRONOS")
            logon_msg.set(FixTag.MsgSeqNum, 1)
            logon_msg.set(FixTag.HeartBtInt, 30)

            writer.write(logon_msg.encode())
            await writer.drain()

            # Read Logon response
            resp_data = await asyncio.wait_for(reader.read(4096), timeout=3.0)
            assert b"35=A" in resp_data
            assert b"49=CHRONOS" in resp_data

            # 2. Send NewOrderSingle (Limit Buy @ $147.00)
            order_msg = FixMessage(FixMsgType.NEW_ORDER_SINGLE)
            order_msg.set(FixTag.SenderCompID, "TCP_ALGO_1")
            order_msg.set(FixTag.TargetCompID, "CHRONOS")
            order_msg.set(FixTag.MsgSeqNum, 2)
            order_msg.set(FixTag.ClOrdID, "TCP_ORD_001")
            order_msg.set(FixTag.Symbol, "SOL-USD")
            order_msg.set(FixTag.Side, FixSide.BUY)
            order_msg.set(FixTag.OrderQty, 100)
            order_msg.set(FixTag.Price, "147.00")
            order_msg.set(FixTag.OrdType, FixOrdType.LIMIT)

            writer.write(order_msg.encode())
            await writer.drain()

            # Read ExecutionReport (New)
            exec_data = await asyncio.wait_for(reader.read(4096), timeout=3.0)
            assert b"35=8" in exec_data
            assert b"150=0" in exec_data  # ExecType=New
            assert b"39=0" in exec_data   # OrdStatus=New
            assert b"11=TCP_ORD_001" in exec_data

            writer.close()
            await writer.wait_closed()

        finally:
            await server.stop()

    asyncio.run(_run())
