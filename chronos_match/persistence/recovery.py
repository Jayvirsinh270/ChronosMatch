"""
ChronosMatch: Deterministic State Machine Recovery Engine.

Restores matching engine state, order book depth, and clearinghouse margin accounts
to the exact microsecond before crash/reboot by replaying binary Write-Ahead Logs
on top of periodic point-in-time snapshots.
"""

from dataclasses import dataclass
import os
import time
from typing import Dict, List, Optional, Any

from chronos_match.market.router import MultiBookRouter, SingleSymbolVenue
from chronos_match.risk.risk_engine import PreTradeRiskEngine
from chronos_match.engine.order_policies import TimeInForce, SelfTradeAction
from chronos_match.protocol import OrderSide, from_fixed_price, TradePacket
from chronos_match.persistence.wal import (
    WriteAheadLog,
    WALRecord,
    WALEventType,
)
from chronos_match.persistence.snapshot import SnapshotManager


@dataclass
class RecoverySummary:
    """Telemetry report produced upon completing deterministic crash recovery."""
    events_replayed: int
    orders_replayed: int
    cancels_replayed: int
    deposits_replayed: int
    corrupted_bytes_truncated: int
    snapshot_applied: Optional[str]
    start_seq: int
    final_seq: int
    elapsed_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "events_replayed": self.events_replayed,
            "orders_replayed": self.orders_replayed,
            "cancels_replayed": self.cancels_replayed,
            "deposits_replayed": self.deposits_replayed,
            "corrupted_bytes_truncated": self.corrupted_bytes_truncated,
            "snapshot_applied": self.snapshot_applied,
            "start_seq": self.start_seq,
            "final_seq": self.final_seq,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


class DeterministicRecoveryEngine:
    """
    Orchestrates cold recovery and state reconstruction across venues and ledgers.
    """

    def __init__(
        self,
        wal_path: str,
        snapshot_dir: Optional[str] = None,
        router: Optional[MultiBookRouter] = None,
        risk_engine: Optional[PreTradeRiskEngine] = None,
    ):
        self.wal_path = wal_path
        self.snapshot_dir = snapshot_dir
        self.router = router or MultiBookRouter()
        self.risk_engine = risk_engine or PreTradeRiskEngine()
        self.snapshot_mgr = SnapshotManager(snapshot_dir) if snapshot_dir else None

    def recover(self) -> RecoverySummary:
        """
        Execute deterministic recovery workflow:
        1. Scan and truncate corrupted trailing crash bytes in WAL file.
        2. Locate and load most recent snapshot.
        3. Replay delta WAL events sequentially.
        """
        t0 = time.perf_counter()

        # Step 1: Repair trailing crash corruption if file exists
        corrupt_bytes = 0
        if os.path.exists(self.wal_path):
            corrupt_bytes = WriteAheadLog.truncate_corrupted_tail(self.wal_path)

        # Step 2: Check for existing snapshot
        start_seq = 1
        snapshot_applied = None
        if self.snapshot_mgr:
            latest_snap = self.snapshot_mgr.find_latest_snapshot()
            if latest_snap:
                meta = self.snapshot_mgr.load_snapshot(
                    latest_snap,
                    router=self.router,
                    risk_engine=self.risk_engine,
                )
                start_seq = meta["last_seq"] + 1
                snapshot_applied = latest_snap

        # Step 3: Stream and replay WAL delta
        events_replayed = 0
        orders_replayed = 0
        cancels_replayed = 0
        deposits_replayed = 0
        final_seq = start_seq - 1

        if os.path.exists(self.wal_path):
            for record in WriteAheadLog.read_records(self.wal_path, from_seq=start_seq):
                final_seq = max(final_seq, record.seq)
                events_replayed += 1

                if record.event_type == WALEventType.ORDER_SUBMIT:
                    self._replay_order_submit(record)
                    orders_replayed += 1
                elif record.event_type == WALEventType.ORDER_CANCEL:
                    self._replay_order_cancel(record)
                    cancels_replayed += 1
                elif record.event_type == WALEventType.ACCOUNT_DEPOSIT:
                    self._replay_account_deposit(record)
                    deposits_replayed += 1

        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        return RecoverySummary(
            events_replayed=events_replayed,
            orders_replayed=orders_replayed,
            cancels_replayed=cancels_replayed,
            deposits_replayed=deposits_replayed,
            corrupted_bytes_truncated=corrupt_bytes,
            snapshot_applied=snapshot_applied,
            start_seq=start_seq,
            final_seq=final_seq,
            elapsed_ms=elapsed_ms,
        )

    def _replay_order_submit(self, record: WALRecord) -> None:
        """Replay single order submission through policy engine and order book."""
        (
            order_id,
            client_id,
            symbol_id,
            side_int,
            price_fixed,
            quantity,
            tif_int,
            post_only_int,
            stp_int,
        ) = record.as_order_submit()

        venue = self.router.get_venue(symbol_id)
        side = OrderSide(side_int)
        tif = TimeInForce(tif_int)
        post_only = bool(post_only_int)
        stp_action = SelfTradeAction(stp_int)

        self.router.client_order_registry[order_id] = client_id
        venue.total_orders += 1
        if order_id >= self.router.order_id_counter:
            self.router.order_id_counter = order_id + 1

        # Apply through policy engine
        policy_result = venue.policy_engine.apply(
            order_id=order_id,
            client_id=client_id,
            price_fixed=price_fixed,
            quantity=quantity,
            side=side_int,
            symbol_id=symbol_id,
            timestamp_ns=record.timestamp_ns,
            tif=tif,
            post_only=post_only,
            stp_action=stp_action,
        )

        price_float = from_fixed_price(price_fixed)

        if not policy_result.accepted:
            return

        filled_shares = policy_result.total_filled
        for m in policy_result.matches:
            qty = m["quantity"]
            venue.total_trades += 1
            venue.total_volume += qty
            trade_p = from_fixed_price(m["execution_price"])
            venue.last_trade_price = trade_p
            venue.high_price = max(venue.high_price, trade_p)
            venue.low_price = min(venue.low_price, trade_p)
            venue.cumulative_notional += trade_p * qty

            # Settle trade in Risk Engine
            buy_c = self.router.client_order_registry.get(m["buy_order_id"], 1)
            sell_c = self.router.client_order_registry.get(m["sell_order_id"], 2)
            self.risk_engine.on_trade_executed(
                buy_client_id=buy_c,
                sell_client_id=sell_c,
                price_float=trade_p,
                quantity=qty,
                symbol_id=symbol_id,
                buy_order_id=m["buy_order_id"],
                sell_order_id=m["sell_order_id"],
            )

            # Update maker resting order fill tracking
            maker_oid = m["buy_order_id"] if side_int == int(OrderSide.SELL) else m["sell_order_id"]
            if maker_oid in venue.user_open_orders:
                venue.user_open_orders[maker_oid]["filled"] += qty
                venue.user_open_orders[maker_oid]["remaining"] = max(
                    0, venue.user_open_orders[maker_oid]["remaining"] - qty
                )
                if venue.user_open_orders[maker_oid]["remaining"] == 0:
                    del venue.user_open_orders[maker_oid]

        # Track resting orders
        if client_id == 1 and tif == TimeInForce.GTC:
            rem = quantity - filled_shares
            if rem > 0:
                self.risk_engine.on_order_accepted(
                    order_id=order_id,
                    client_id=client_id,
                    side=side_int,
                    price_float=price_float,
                    quantity=rem,
                    symbol_id=symbol_id,
                )
                venue.user_open_orders[order_id] = {
                    "order_id": order_id,
                    "symbol": venue.info.ticker,
                    "side": "BUY" if side_int == int(OrderSide.BUY) else "SELL",
                    "price": price_float,
                    "quantity": quantity,
                    "filled": filled_shares,
                    "remaining": rem,
                    "tif": tif.name,
                    "post_only": post_only,
                    "time": time.strftime("%H:%M:%S"),
                }

    def _replay_order_cancel(self, record: WALRecord) -> None:
        """Replay cancellation of an active order."""
        order_id, client_id, symbol_id = record.as_order_cancel()
        venue = self.router.get_venue(symbol_id)

        # Cancel on book
        venue.book.cancel_order(order_id)

        # Release reserved funds in Risk Engine
        self.risk_engine.on_order_cancelled(order_id=order_id, client_id=client_id)

        # Remove from open order tracker
        venue.user_open_orders.pop(order_id, None)

    def _replay_account_deposit(self, record: WALRecord) -> None:
        """Replay cash or asset balance credit."""
        client_id, cash_usd, symbol_id, shares = record.as_account_deposit()
        acc = self.risk_engine.get_or_create_account(
            client_id=client_id,
            initial_cash=0.0,
            initial_shares=0,
        )
        if cash_usd > 0:
            acc.cash_balance += cash_usd
        if shares != 0 and symbol_id > 0:
            acc.deposit(shares=shares, symbol_id=symbol_id)
