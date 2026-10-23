"""
ChronosMatch: Financial Information eXchange (FIX 4.2) Protocol Engine.

Implements standard tag-value protocol framing, checksum calculation,
high-throughput parsing, and message generation for institutional exchanges.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union


SOH = b"\x01"  # Standard Start of Header / delimiter
SOH_CHR = "\x01"


class FixTag:
    """Standard FIX 4.2 Field Tags."""
    BeginString = 8
    BodyLength = 9
    CheckSum = 10
    ClOrdID = 11
    CumQty = 14
    Currency = 15
    ExecID = 17
    ExecInst = 18
    ExecRefID = 19
    ExecTransType = 20
    HandlInst = 21
    MsgSeqNum = 34
    MsgType = 35
    OrderID = 37
    OrderQty = 38
    OrdStatus = 39
    OrdType = 40
    OrigClOrdID = 41
    Price = 44
    SecurityID = 48
    SenderCompID = 49
    SendingTime = 52
    Side = 54
    Symbol = 55
    TargetCompID = 56
    Text = 58
    TimeInForce = 59
    TransactTime = 60
    LastPx = 31
    LastShares = 32
    AvgPx = 6
    LeavesQty = 151
    ExecType = 150
    HeartBtInt = 108
    TestReqID = 112
    OrdRejReason = 103
    CxlRejReason = 102
    CxlRejResponseTo = 434


class FixMsgType:
    """FIX 4.2 Message Types (Tag 35)."""
    HEARTBEAT = "0"
    TEST_REQUEST = "1"
    RESEND_REQUEST = "2"
    REJECT = "3"
    SEQUENCE_RESET = "4"
    LOGOUT = "5"
    LOGON = "A"
    NEW_ORDER_SINGLE = "D"
    ORDER_CANCEL_REQUEST = "F"
    ORDER_STATUS_REQUEST = "H"
    EXECUTION_REPORT = "8"
    ORDER_CANCEL_REJECT = "9"


class FixSide:
    BUY = "1"
    SELL = "2"


class FixOrdType:
    MARKET = "1"
    LIMIT = "2"


class FixTimeInForce:
    DAY = "0"
    GTC = "1"
    IOC = "3"
    FOK = "4"


class FixExecType:
    NEW = "0"
    PARTIAL_FILL = "1"
    FILL = "2"
    DONE_FOR_DAY = "3"
    CANCELLED = "4"
    REPLACE = "5"
    PENDING_CANCEL = "6"
    STOPPED = "7"
    REJECTED = "8"


class FixOrdStatus:
    NEW = "0"
    PARTIALLY_FILLED = "1"
    FILLED = "2"
    DONE_FOR_DAY = "3"
    CANCELLED = "4"
    PENDING_CANCEL = "6"
    STOPPED = "7"
    REJECTED = "8"


class FixMessage:
    """
    In-memory representation of a FIX 4.2 Message.
    Maintains ordered fields and provides high-speed serialization/deserialization.
    """

    def __init__(self, msg_type: Optional[str] = None):
        self.fields: Dict[int, str] = {}
        if msg_type is not None:
            self.set(FixTag.MsgType, msg_type)

    def set(self, tag: int, value: Any) -> "FixMessage":
        """Set a tag value as string."""
        self.fields[tag] = str(value)
        return self

    def get(self, tag: int, default: Optional[str] = None) -> Optional[str]:
        """Retrieve tag value as string."""
        return self.fields.get(tag, default)

    def get_int(self, tag: int, default: int = 0) -> int:
        val = self.get(tag)
        return int(val) if val is not None else default

    def get_float(self, tag: int, default: float = 0.0) -> float:
        val = self.get(tag)
        return float(val) if val is not None else default

    @property
    def msg_type(self) -> Optional[str]:
        return self.get(FixTag.MsgType)

    @property
    def seq_num(self) -> int:
        return self.get_int(FixTag.MsgSeqNum, 0)

    @staticmethod
    def current_sending_time() -> str:
        """Formatted UTC timestamp: YYYYMMDD-HH:MM:SS.sss"""
        now = datetime.now(timezone.utc)
        return now.strftime("%Y%m%d-%H:%M:%S.%f")[:-3]

    @staticmethod
    def calculate_checksum(data: bytes) -> str:
        """Calculates 3-digit ASCII CheckSum (Tag 10) modulo 256."""
        total = sum(data) % 256
        return f"{total:03d}"

    def encode(self, sender_comp_id: Optional[str] = None, target_comp_id: Optional[str] = None, seq_num: Optional[int] = None) -> bytes:
        """
        Serialize into standard FIX 4.2 wire format:
        8=FIX.4.2|9=Len|35=Type|...|10=Checksum|
        """
        if sender_comp_id:
            self.set(FixTag.SenderCompID, sender_comp_id)
        if target_comp_id:
            self.set(FixTag.TargetCompID, target_comp_id)
        if seq_num is not None:
            self.set(FixTag.MsgSeqNum, seq_num)
        if FixTag.SendingTime not in self.fields:
            self.set(FixTag.SendingTime, self.current_sending_time())

        # Construct Body (all tags except 8=BeginString, 9=BodyLength, and 10=CheckSum)
        body_parts: List[bytes] = []

        # Tag 35 must come first in body by FIX convention
        msg_t = self.get(FixTag.MsgType, FixMsgType.HEARTBEAT)
        body_parts.append(f"{FixTag.MsgType}={msg_t}".encode("ascii"))

        for tag, val in self.fields.items():
            if tag in (FixTag.BeginString, FixTag.BodyLength, FixTag.CheckSum, FixTag.MsgType):
                continue
            body_parts.append(f"{tag}={val}".encode("ascii"))

        body_bytes = SOH.join(body_parts) + SOH
        body_len = len(body_bytes)

        # Header prefix: 8=FIX.4.2|9=Length|
        header_bytes = f"{FixTag.BeginString}=FIX.4.2{SOH_CHR}{FixTag.BodyLength}={body_len}{SOH_CHR}".encode("ascii")

        # Full message without checksum
        pre_checksum = header_bytes + body_bytes
        checksum_val = self.calculate_checksum(pre_checksum)

        return pre_checksum + f"{FixTag.CheckSum}={checksum_val}{SOH_CHR}".encode("ascii")

    @classmethod
    def parse(cls, raw: Union[bytes, str]) -> "FixMessage":
        """
        Parse raw FIX byte buffer or string into FixMessage.
        Validates delimiter and checksum.
        """
        if isinstance(raw, str):
            raw_bytes = raw.encode("ascii")
        else:
            raw_bytes = raw

        # Split on SOH (or fallback to '|' for readable test strings)
        delim = SOH if SOH in raw_bytes else b"|"
        tokens = raw_bytes.split(delim)

        msg = cls()
        for tok in tokens:
            if not tok:
                continue
            if b"=" not in tok:
                continue
            k_bytes, v_bytes = tok.split(b"=", 1)
            try:
                tag = int(k_bytes.strip())
                val = v_bytes.strip().decode("ascii", errors="replace")
                msg.set(tag, val)
            except ValueError:
                continue

        # Optional checksum verification if CheckSum is provided and SOH was delimiter
        checksum_field = msg.get(FixTag.CheckSum)
        if checksum_field is not None and delim == SOH:
            idx = raw_bytes.rfind(b"10=")
            if idx > 0:
                expected_sum = cls.calculate_checksum(raw_bytes[:idx])
                if checksum_field != expected_sum:
                    raise ValueError(f"Checksum mismatch: received {checksum_field}, computed {expected_sum}")

        return msg

    def to_readable_string(self) -> str:
        """Convert message to readable pipe-delimited format."""
        encoded = self.encode()
        return encoded.replace(SOH, b"|").decode("ascii")


def make_execution_report(
    order_id: int,
    cl_ord_id: str,
    exec_id: str,
    exec_type: str,
    ord_status: str,
    symbol: str,
    side: str,
    order_qty: int,
    price: float,
    leaves_qty: int,
    cum_qty: int,
    avg_px: float = 0.0,
    last_px: float = 0.0,
    last_shares: int = 0,
    reject_reason: Optional[str] = None,
) -> FixMessage:
    """Construct an institutional ExecutionReport (35=8)."""
    msg = FixMessage(FixMsgType.EXECUTION_REPORT)
    msg.set(FixTag.OrderID, order_id)
    msg.set(FixTag.ClOrdID, cl_ord_id)
    msg.set(FixTag.ExecID, exec_id)
    msg.set(FixTag.ExecType, exec_type)
    msg.set(FixTag.OrdStatus, ord_status)
    msg.set(FixTag.Symbol, symbol)
    msg.set(FixTag.Side, side)
    msg.set(FixTag.OrderQty, order_qty)
    msg.set(FixTag.Price, f"{price:.2f}")
    msg.set(FixTag.LeavesQty, leaves_qty)
    msg.set(FixTag.CumQty, cum_qty)
    msg.set(FixTag.AvgPx, f"{avg_px:.2f}")
    if last_shares > 0:
        msg.set(FixTag.LastShares, last_shares)
        msg.set(FixTag.LastPx, f"{last_px:.2f}")
    if reject_reason:
        msg.set(FixTag.Text, reject_reason)
        msg.set(FixTag.OrdRejReason, "99")  # Other/Risk Reject
    return msg
