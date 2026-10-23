"""
ChronosMatch: FIX 4.2 Session State Machine.

Maintains bidirectional message sequencing, logon handshakes, keepalive heartbeats,
and client connection lifecycle for institutional trading counterparties.
"""

import time
from typing import Dict, List, Optional, Tuple, Any

from chronos_match.gateway.fix_protocol import (
    FixMessage,
    FixTag,
    FixMsgType,
    FixExecType,
    FixOrdStatus,
    make_execution_report,
)


class FixSession:
    """
    State machine for a single authenticated FIX 4.2 TCP connection.
    """

    def __init__(
        self,
        target_comp_id: str = "CHRONOS",
        sender_comp_id: Optional[str] = None,
        heartbeat_interval_sec: int = 30,
    ):
        self.target_comp_id = target_comp_id  # Exchange ID
        self.sender_comp_id = sender_comp_id  # Client ID
        self.heartbeat_interval_sec = heartbeat_interval_sec

        self.in_seq_num = 1
        self.out_seq_num = 1
        self.authenticated = False
        self.last_received_time = time.time()
        self.last_sent_time = time.time()

        # Client Order ID mapping (ClOrdID -> internal order_id)
        self.cl_ord_to_order_id: Dict[str, int] = {}
        self.order_id_to_cl_ord: Dict[int, str] = {}

    def next_out_seq(self) -> int:
        seq = self.out_seq_num
        self.out_seq_num += 1
        return seq

    def format_outgoing(self, msg: FixMessage) -> bytes:
        """Stamp session header metadata and produce wire bytes."""
        msg.set(FixTag.SenderCompID, self.target_comp_id)
        msg.set(FixTag.TargetCompID, self.sender_comp_id or "CLIENT")
        msg.set(FixTag.MsgSeqNum, self.next_out_seq())
        msg.set(FixTag.SendingTime, FixMessage.current_sending_time())
        self.last_sent_time = time.time()
        return msg.encode()

    def handle_incoming(self, msg: FixMessage) -> List[FixMessage]:
        """
        Process incoming administrative messages (Logon, Heartbeat, TestRequest, Logout).
        Returns list of response messages to send immediately back to client.
        """
        self.last_received_time = time.time()
        msg_type = msg.msg_type

        # Check sequence number
        seq_num = msg.seq_num
        if seq_num > 0:
            self.in_seq_num = max(self.in_seq_num, seq_num + 1)

        responses: List[FixMessage] = []

        if msg_type == FixMsgType.LOGON:
            client_comp = msg.get(FixTag.SenderCompID, "ANON_CLIENT")
            self.sender_comp_id = client_comp
            self.authenticated = True
            hb_int = msg.get_int(FixTag.HeartBtInt, self.heartbeat_interval_sec)
            self.heartbeat_interval_sec = hb_int

            # Respond with Logon confirmation
            resp = FixMessage(FixMsgType.LOGON)
            resp.set(FixTag.HeartBtInt, hb_int)
            responses.append(resp)

        elif msg_type == FixMsgType.TEST_REQUEST:
            test_req_id = msg.get(FixTag.TestReqID, "")
            # Must reply with Heartbeat containing TestReqID
            resp = FixMessage(FixMsgType.HEARTBEAT)
            if test_req_id:
                resp.set(FixTag.TestReqID, test_req_id)
            responses.append(resp)

        elif msg_type == FixMsgType.HEARTBEAT:
            # Keepalive acknowledge; no response needed
            pass

        elif msg_type == FixMsgType.LOGOUT:
            self.authenticated = False
            # Acknowledge logout
            resp = FixMessage(FixMsgType.LOGOUT)
            responses.append(resp)

        return responses

    def make_order_cancel_reject(self, cl_ord_id: str, orig_cl_ord_id: str, reason: str = "Order not found") -> FixMessage:
        """Construct OrderCancelReject (35=9)."""
        msg = FixMessage(FixMsgType.ORDER_CANCEL_REJECT)
        msg.set(FixTag.OrderID, "NONE")
        msg.set(FixTag.ClOrdID, cl_ord_id)
        msg.set(FixTag.OrigClOrdID, orig_cl_ord_id)
        msg.set(FixTag.OrdStatus, FixOrdStatus.REJECTED)
        msg.set(FixTag.CxlRejResponseTo, "1")  # Order Cancel Request
        msg.set(FixTag.CxlRejReason, "1")      # Unknown order
        msg.set(FixTag.Text, reason)
        return msg
