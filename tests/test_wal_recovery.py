"""
ChronosMatch: Test Suite for Phase 4 (Write-Ahead Log & Deterministic Crash Recovery).

Validates:
1. Binary WAL serialization, monotonic sequencing, and CRC32 payload checksum integrity.
2. Abrupt crash simulation and automated truncation of corrupted trailing bytes.
3. Atomic point-in-time snapshot serialization and restoration.
4. End-to-end deterministic state recovery across order books, resting queues, and clearinghouse ledgers.
5. Snapshot compaction + incremental WAL delta replay.
6. High-throughput binary serialization performance benchmark.
7. Web trading server WAL status and snapshot methods.
"""

import os
import shutil
import tempfile
import time
import pytest

from chronos_match.market.router import MultiBookRouter, SymbolInfo
from chronos_match.risk.risk_engine import PreTradeRiskEngine
from chronos_match.protocol import OrderSide, to_fixed_price, from_fixed_price
from chronos_match.engine.order_policies import TimeInForce, SelfTradeAction
from chronos_match.persistence.wal import (
    WriteAheadLog,
    WALRecord,
    WALEventType,
)
from chronos_match.persistence.snapshot import SnapshotManager
from chronos_match.persistence.recovery import (
    DeterministicRecoveryEngine,
    RecoverySummary,
)
from chronos_match.web.server import WebTradingServer


@pytest.fixture
def temp_dir():
    """Create and cleanly tear down a temporary test directory."""
    d = tempfile.mkdtemp(prefix="chronos_test_wal_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_wal_binary_append_and_read(temp_dir):
    """Verify binary serialization, sequence numbers, and CRC32 payload checks."""
    wal_path = os.path.join(temp_dir, "test.wal")
    wal = WriteAheadLog(filepath=wal_path, sync_mode="immediate")

    # Append 3 distinct event types
    s1 = wal.append_order_submit(
        order_id=101,
        client_id=1,
        symbol_id=1,
        side=int(OrderSide.BUY),
        price=to_fixed_price(150.25),
        quantity=500,
        tif=int(TimeInForce.GTC),
        post_only=0,
        stp_action=int(SelfTradeAction.CANCEL_AGGRESSOR),
    )
    s2 = wal.append_order_cancel(
        order_id=101,
        client_id=1,
        symbol_id=1,
    )
    s3 = wal.append_account_deposit(
        client_id=1,
        cash_usd=50_000.0,
        symbol_id=2,
        shares=10,
    )
    wal.close()

    assert s1 == 1
    assert s2 == 2
    assert s3 == 3

    # Read back records
    records = list(WriteAheadLog.read_records(wal_path))
    assert len(records) == 3

    # Verify Record 1 (Order Submit)
    r1 = records[0]
    assert r1.seq == 1
    assert r1.event_type == WALEventType.ORDER_SUBMIT
    oid, cid, sym_id, side, p, q, tif, po, stp = r1.as_order_submit()
    assert oid == 101
    assert cid == 1
    assert sym_id == 1
    assert side == int(OrderSide.BUY)
    assert from_fixed_price(p) == 150.25
    assert q == 500
    assert tif == int(TimeInForce.GTC)
    assert po == 0
    assert stp == int(SelfTradeAction.CANCEL_AGGRESSOR)

    # Verify Record 2 (Order Cancel)
    r2 = records[1]
    assert r2.seq == 2
    assert r2.event_type == WALEventType.ORDER_CANCEL
    oid, cid, sym_id = r2.as_order_cancel()
    assert oid == 101
    assert cid == 1
    assert sym_id == 1

    # Verify Record 3 (Account Deposit)
    r3 = records[2]
    assert r3.seq == 3
    assert r3.event_type == WALEventType.ACCOUNT_DEPOSIT
    cid, cash, sym_id, shares = r3.as_account_deposit()
    assert cid == 1
    assert cash == 50_000.0
    assert sym_id == 2
    assert shares == 10


def test_wal_crash_truncation_corrupted_tail(temp_dir):
    """Simulate mid-write crash leaving trailing garbage bytes and test auto-repair."""
    wal_path = os.path.join(temp_dir, "crash_sim.wal")
    wal = WriteAheadLog(filepath=wal_path, sync_mode="immediate")

    for i in range(1, 11):
        wal.append_order_submit(
            order_id=i,
            client_id=1,
            symbol_id=1,
            side=int(OrderSide.BUY),
            price=to_fixed_price(150.0 + i),
            quantity=100,
        )
    wal.close()

    # Simulate abrupt power-loss: corrupt the last write by appending 13 random half-bytes
    with open(wal_path, "ab") as f:
        f.write(b"\xCC\xDE\xAD\xBE\xEF\x01\x02\x03\x04\x05\x06\x07\x08")

    initial_size = os.path.getsize(wal_path)

    # Scans and truncates trailing corrupted bytes
    corrupted_bytes = WriteAheadLog.truncate_corrupted_tail(wal_path)
    assert corrupted_bytes == 13
    assert os.path.getsize(wal_path) == initial_size - 13

    # All 10 valid records must read back cleanly
    records = list(WriteAheadLog.read_records(wal_path))
    assert len(records) == 10
    assert records[-1].seq == 10


def test_snapshot_atomic_creation_and_restore(temp_dir):
    """Verify snapshot serialization of venues, open orders, and clearinghouse accounts."""
    snap_mgr = SnapshotManager(snapshot_dir=temp_dir)
    router = MultiBookRouter()
    risk = PreTradeRiskEngine()

    v1 = router.register_symbol(SymbolInfo(1, "SOL-USD", "SOL", "USD", 0.01, 1, 150.0))
    v2 = router.register_symbol(SymbolInfo(2, "BTC-USD", "BTC", "USD", 0.50, 1, 65000.0))

    # Add mock open order
    v1.user_open_orders[999] = {
        "order_id": 999,
        "symbol": "SOL-USD",
        "side": "BUY",
        "price": 149.50,
        "quantity": 250,
        "filled": 0,
        "remaining": 250,
        "tif": "GTC",
        "post_only": False,
        "time": "12:00:00",
    }
    v1.total_orders = 42
    v1.total_trades = 15

    # Fund account
    acc = risk.get_or_create_account(1, initial_cash=2_500_000.0)
    acc.deposit(shares=25, symbol_id=2)

    # Create atomic snapshot
    snap_path = snap_mgr.create_snapshot(router=router, last_seq=150, risk_engine=risk)
    assert os.path.exists(snap_path)
    assert snap_mgr.find_latest_snapshot() == snap_path

    # Restore into fresh engine
    fresh_router = MultiBookRouter()
    fresh_risk = PreTradeRiskEngine()
    meta = snap_mgr.load_snapshot(snap_path, router=fresh_router, risk_engine=fresh_risk)

    assert meta["last_seq"] == 150
    assert meta["restored_venues"] == 2
    assert meta["restored_accounts"] == 1

    restored_v1 = fresh_router.get_venue(1)
    assert restored_v1.total_orders == 42
    assert restored_v1.total_trades == 15
    assert 999 in restored_v1.user_open_orders
    assert restored_v1.user_open_orders[999]["remaining"] == 250

    restored_acc = fresh_risk.get_or_create_account(1)
    assert restored_acc.cash_balance == 2_500_000.0
    assert restored_acc.positions.get(2) == 25


def test_deterministic_full_replay_recovery(temp_dir):
    """
    Test zero-loss deterministic recovery:
    Executes multiple orders, matches, and cancels in a venue, crashes the engine,
    replays WAL from scratch, and verifies exact match of books, trades, and balances.
    """
    wal_path = os.path.join(temp_dir, "chronos_test_engine.wal")
    wal = WriteAheadLog(filepath=wal_path, sync_mode="immediate")

    # Reference Engine
    orig_router = MultiBookRouter()
    orig_risk = PreTradeRiskEngine()
    venue = orig_router.register_symbol(SymbolInfo(1, "SOL-USD", "SOL", "USD", 0.01, 1, 150.0), seed=False)
    acc1 = orig_risk.get_or_create_account(1, initial_cash=0.0, initial_shares=0, symbol_id=1)
    acc2 = orig_risk.get_or_create_account(2, initial_cash=0.0, initial_shares=0, symbol_id=1)

    # Log initial funding to WAL and fund accounts
    wal.append_account_deposit(1, cash_usd=1_000_000.0, symbol_id=1, shares=10_000)
    wal.append_account_deposit(2, cash_usd=1_000_000.0, symbol_id=1, shares=10_000)
    acc1.cash_balance += 1_000_000.0
    acc1.deposit(shares=10_000, symbol_id=1)
    acc2.cash_balance += 1_000_000.0
    acc2.deposit(shares=10_000, symbol_id=1)

    # 1. Client 1 places resting Buy limit at $149.00 for 100 shares
    oid_1 = 501
    wal.append_order_submit(
        order_id=oid_1, client_id=1, symbol_id=1, side=int(OrderSide.BUY),
        price=to_fixed_price(149.00), quantity=100, tif=int(TimeInForce.GTC),
    )
    venue.policy_engine.apply(
        order_id=oid_1, client_id=1, price_fixed=to_fixed_price(149.00),
        quantity=100, side=int(OrderSide.BUY), symbol_id=1, timestamp_ns=time.perf_counter_ns(),
        tif=TimeInForce.GTC, post_only=False, stp_action=SelfTradeAction.ALLOW,
    )
    venue.user_open_orders[oid_1] = {
        "order_id": oid_1, "symbol": "SOL-USD", "side": "BUY", "price": 149.00,
        "quantity": 100, "filled": 0, "remaining": 100, "tif": "GTC", "post_only": False, "time": "10:00:00"
    }

    # 2. Client 1 places resting Buy limit at $148.50 for 200 shares
    oid_2 = 502
    wal.append_order_submit(
        order_id=oid_2, client_id=1, symbol_id=1, side=int(OrderSide.BUY),
        price=to_fixed_price(148.50), quantity=200, tif=int(TimeInForce.GTC),
    )
    venue.policy_engine.apply(
        order_id=oid_2, client_id=1, price_fixed=to_fixed_price(148.50),
        quantity=200, side=int(OrderSide.BUY), symbol_id=1, timestamp_ns=time.perf_counter_ns(),
        tif=TimeInForce.GTC, post_only=False, stp_action=SelfTradeAction.ALLOW,
    )
    venue.user_open_orders[oid_2] = {
        "order_id": oid_2, "symbol": "SOL-USD", "side": "BUY", "price": 148.50,
        "quantity": 200, "filled": 0, "remaining": 200, "tif": "GTC", "post_only": False, "time": "10:00:01"
    }

    # 3. Client 2 places aggressive Sell limit at $149.00 for 50 shares (fills 50 against oid_1)
    oid_3 = 503
    wal.append_order_submit(
        order_id=oid_3, client_id=2, symbol_id=1, side=int(OrderSide.SELL),
        price=to_fixed_price(149.00), quantity=50, tif=int(TimeInForce.IOC),
    )
    res3 = venue.policy_engine.apply(
        order_id=oid_3, client_id=2, price_fixed=to_fixed_price(149.00),
        quantity=50, side=int(OrderSide.SELL), symbol_id=1, timestamp_ns=time.perf_counter_ns(),
        tif=TimeInForce.IOC, post_only=False, stp_action=SelfTradeAction.ALLOW,
    )
    assert res3.total_filled == 50
    orig_risk.on_trade_executed(
        buy_client_id=1, sell_client_id=2, price_float=149.00, quantity=50, symbol_id=1,
        buy_order_id=oid_1, sell_order_id=oid_3,
    )
    venue.user_open_orders[oid_1]["remaining"] = 50
    venue.user_open_orders[oid_1]["filled"] = 50

    # 4. Client 1 cancels order oid_2
    wal.append_order_cancel(order_id=oid_2, client_id=1, symbol_id=1)
    venue.book.cancel_order(oid_2)
    orig_risk.on_order_cancelled(oid_2, client_id=1)
    venue.user_open_orders.pop(oid_2, None)

    wal.close()

    # Capture expected post-run state
    expected_l2 = venue.book.get_l2_snapshot(depth=5)
    expected_open_keys = list(venue.user_open_orders.keys())
    expected_acc1_cash = acc1.cash_balance
    expected_acc1_pos = acc1.positions.get(1)

    # 💥 SIMULATE HARD REBOOT / RECOVERY ENGINE
    recovered_router = MultiBookRouter()
    recovered_router.register_symbol(SymbolInfo(1, "SOL-USD", "SOL", "USD", 0.01, 1, 150.0), seed=False)
    recovered_risk = PreTradeRiskEngine()

    recovery_engine = DeterministicRecoveryEngine(
        wal_path=wal_path,
        router=recovered_router,
        risk_engine=recovered_risk,
    )
    summary: RecoverySummary = recovery_engine.recover()

    assert summary.orders_replayed == 3
    assert summary.cancels_replayed == 1
    assert summary.deposits_replayed == 2
    assert summary.events_replayed == 6

    # Verify order book depth equivalence
    recovered_venue = recovered_router.get_venue(1)
    recovered_l2 = recovered_venue.book.get_l2_snapshot(depth=5)

    # Compare top bids
    assert len(recovered_l2["bids"]) == len(expected_l2["bids"])
    for orig_b, rec_b in zip(expected_l2["bids"], recovered_l2["bids"]):
        assert orig_b["price"] == rec_b["price"]
        assert orig_b["volume"] == rec_b["volume"]

    # Verify open orders
    assert list(recovered_venue.user_open_orders.keys()) == expected_open_keys
    assert recovered_venue.user_open_orders[oid_1]["remaining"] == 50

    # Verify ledger balances
    rec_acc1 = recovered_risk.get_or_create_account(1)
    assert rec_acc1.cash_balance == expected_acc1_cash
    assert rec_acc1.positions.get(1) == expected_acc1_pos


def test_snapshot_plus_incremental_wal_replay(temp_dir):
    """Test checkpoint snapshot compaction followed by replaying subsequent delta WAL entries."""
    wal_path = os.path.join(temp_dir, "incremental.wal")
    snap_dir = os.path.join(temp_dir, "snaps")
    wal = WriteAheadLog(filepath=wal_path, sync_mode="immediate")
    snap_mgr = SnapshotManager(snapshot_dir=snap_dir)

    router = MultiBookRouter()
    risk = PreTradeRiskEngine()
    venue = router.register_symbol(SymbolInfo(1, "SOL-USD", "SOL", "USD", 0.01, 1, 150.0))

    # Phase A: Place 5 orders and take snapshot at sequence 5
    for i in range(1, 6):
        oid = 1000 + i
        wal.append_order_submit(
            order_id=oid, client_id=1, symbol_id=1, side=int(OrderSide.BUY),
            price=to_fixed_price(140.0 + i), quantity=100, tif=int(TimeInForce.GTC),
        )
        venue.policy_engine.apply(
            order_id=oid, client_id=1, price_fixed=to_fixed_price(140.0 + i),
            quantity=100, side=int(OrderSide.BUY), symbol_id=1, timestamp_ns=time.perf_counter_ns(),
            tif=TimeInForce.GTC, post_only=False, stp_action=SelfTradeAction.ALLOW,
        )
        venue.user_open_orders[oid] = {
            "order_id": oid, "symbol": "SOL-USD", "side": "BUY", "price": 140.0 + i,
            "quantity": 100, "filled": 0, "remaining": 100, "tif": "GTC", "post_only": False, "time": "12:00:00"
        }

    # Take Snapshot at seq=5
    snap_path = snap_mgr.create_snapshot(router=router, last_seq=5, risk_engine=risk)
    wal.append_snapshot_marker(snapshot_seq=5, snapshot_path=snap_path)

    # Phase B: Place 3 more orders (seq 7, 8, 9)
    for i in range(6, 9):
        oid = 1000 + i
        wal.append_order_submit(
            order_id=oid, client_id=1, symbol_id=1, side=int(OrderSide.BUY),
            price=to_fixed_price(140.0 + i), quantity=100, tif=int(TimeInForce.GTC),
        )
        venue.policy_engine.apply(
            order_id=oid, client_id=1, price_fixed=to_fixed_price(140.0 + i),
            quantity=100, side=int(OrderSide.BUY), symbol_id=1, timestamp_ns=time.perf_counter_ns(),
            tif=TimeInForce.GTC, post_only=False, stp_action=SelfTradeAction.ALLOW,
        )
        venue.user_open_orders[oid] = {
            "order_id": oid, "symbol": "SOL-USD", "side": "BUY", "price": 140.0 + i,
            "quantity": 100, "filled": 0, "remaining": 100, "tif": "GTC", "post_only": False, "time": "12:00:00"
        }

    wal.close()

    # Recover with snapshot + delta WAL
    fresh_router = MultiBookRouter()
    fresh_risk = PreTradeRiskEngine()
    rec_engine = DeterministicRecoveryEngine(
        wal_path=wal_path,
        snapshot_dir=snap_dir,
        router=fresh_router,
        risk_engine=fresh_risk,
    )
    summary = rec_engine.recover()

    assert summary.snapshot_applied is not None
    assert summary.start_seq == 6  # Replayed from seq 6 onward!
    assert summary.orders_replayed == 3  # Only the 3 orders after snapshot were replayed
    assert len(fresh_router.get_venue(1).user_open_orders) == 8


def test_wal_serialization_throughput_benchmark(temp_dir):
    """Benchmark raw binary append throughput (> 20,000 ops/sec)."""
    wal_path = os.path.join(temp_dir, "bench.wal")
    wal = WriteAheadLog(filepath=wal_path, sync_mode="batched", batch_size=2000)

    count = 10_000
    t0 = time.perf_counter()
    for i in range(count):
        wal.append_order_submit(
            order_id=i + 1,
            client_id=1,
            symbol_id=1,
            side=int(OrderSide.BUY),
            price=1500000,
            quantity=100,
        )
    wal.close()
    elapsed = time.perf_counter() - t0

    throughput = count / max(0.0001, elapsed)
    assert throughput > 20_000, f"Expected >20k ops/sec, got {throughput:.0f}"


def test_web_server_wal_and_snapshot_methods(temp_dir):
    """Test web server integration with WAL status and snapshot creation directly."""
    test_wal_path = os.path.join(temp_dir, "api_test.wal")
    test_snap_dir = os.path.join(temp_dir, "api_snaps")

    custom_server = WebTradingServer(
        wal_path=test_wal_path,
        snapshot_dir=test_snap_dir,
        enable_wal=True,
    )

    try:
        assert custom_server.enable_wal is True
        assert custom_server.wal is not None
        assert custom_server.wal.next_seq == 1

        # Place order on server and verify WAL advances
        resp = custom_server.place_order(
            side_str="BUY",
            type_str="LIMIT",
            price_float=149.00,
            quantity=100,
            symbol="SOL-USD",
        )
        assert resp["success"] is True
        assert custom_server.wal.next_seq == 2

        # Cancel order and verify WAL advances
        oid = resp["order_id"]
        c_resp = custom_server.cancel_user_order(order_id=oid, symbol="SOL-USD")
        assert c_resp["success"] is True
        assert custom_server.wal.next_seq == 3

        # Create snapshot
        seq = custom_server.wal.next_seq - 1
        snap_path = custom_server.snapshot_mgr.create_snapshot(
            router=custom_server.router,
            last_seq=seq,
            risk_engine=custom_server.risk_engine,
        )
        assert os.path.exists(snap_path)

    finally:
        custom_server.wal.close()
