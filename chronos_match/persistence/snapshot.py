"""
ChronosMatch: Deterministic State Snapshot Engine.

Takes point-in-time compact checkpoints of the multi-asset matching engine,
active resting order books, and clearinghouse accounts.
Uses atomic rename for crash-safe non-corrupting snapshot creation.
"""

from dataclasses import asdict
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from chronos_match.market.router import MultiBookRouter, SymbolInfo, SingleSymbolVenue
from chronos_match.risk.risk_engine import PreTradeRiskEngine, AccountBalance
from chronos_match.protocol import to_fixed_price, from_fixed_price, OrderSide


class SnapshotManager:
    """
    Manages generation, storage, and atomic loading of exchange state snapshots.
    """

    SNAPSHOT_MAGIC = "CHRONOS_SNAPSHOT_V1"

    def __init__(self, snapshot_dir: str = "chronos_snapshots"):
        self.snapshot_dir = snapshot_dir
        os.makedirs(self.snapshot_dir, exist_ok=True)

    def create_snapshot(
        self,
        router: MultiBookRouter,
        last_seq: int,
        risk_engine: Optional[PreTradeRiskEngine] = None,
        custom_filename: Optional[str] = None,
    ) -> str:
        """
        Create a point-in-time atomic checkpoint file.
        Returns the absolute filepath of the created snapshot.
        """
        now_ns = time.perf_counter_ns()
        now_str = time.strftime("%Y%m%d_%H%M%S")
        filename = custom_filename or f"snapshot_seq_{last_seq}_{now_str}.snap"
        final_path = os.path.join(self.snapshot_dir, filename)
        tmp_path = final_path + ".tmp"

        # Serialize risk engine accounts
        accounts_data: List[Dict[str, Any]] = []
        if risk_engine is not None:
            for cid, acc in risk_engine.accounts.items():
                accounts_data.append({
                    "client_id": acc.client_id,
                    "cash_balance": acc.cash_balance,
                    "cash_reserved": acc.cash_reserved,
                    "positions": dict(acc.positions),
                    "positions_reserved": dict(acc.positions_reserved),
                    "active_order_reservations": dict(acc.active_order_reservations),
                })

        # Serialize venues and their resting orders
        venues_data: List[Dict[str, Any]] = []
        for symbol_id, venue in router.venues_by_id.items():
            # Gather open orders tracked in venue
            open_orders = list(venue.user_open_orders.values())
            venues_data.append({
                "symbol_id": venue.info.symbol_id,
                "ticker": venue.info.ticker,
                "base_asset": venue.info.base_asset,
                "quote_asset": venue.info.quote_asset,
                "tick_size": venue.info.tick_size,
                "lot_size": venue.info.lot_size,
                "initial_price": venue.info.initial_price,
                "price_scale": venue.info.price_scale,
                "total_orders": venue.total_orders,
                "total_trades": venue.total_trades,
                "total_volume": venue.total_volume,
                "last_trade_price": venue.last_trade_price,
                "high_price": venue.high_price,
                "low_price": venue.low_price,
                "cumulative_notional": venue.cumulative_notional,
                "user_open_orders": open_orders,
            })

        payload = {
            "magic": self.SNAPSHOT_MAGIC,
            "version": 1,
            "last_seq": last_seq,
            "timestamp_ns": now_ns,
            "order_id_counter": router.order_id_counter,
            "client_order_registry": {str(k): v for k, v in router.client_order_registry.items()},
            "venues": venues_data,
            "accounts": accounts_data,
        }

        # Write atomically to temporary file, flush to OS, then rename
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, final_path)
        return final_path

    def find_latest_snapshot(self) -> Optional[str]:
        """Find the latest snapshot file in the directory by last_seq."""
        if not os.path.exists(self.snapshot_dir):
            return None

        candidates = []
        for fname in os.listdir(self.snapshot_dir):
            if fname.endswith(".snap") and not fname.endswith(".tmp"):
                fpath = os.path.join(self.snapshot_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if data.get("magic") == self.SNAPSHOT_MAGIC:
                            candidates.append((data.get("last_seq", 0), fpath))
                except Exception:
                    continue

        if not candidates:
            return None

        # Sort descending by sequence number
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def load_snapshot(
        self,
        filepath: str,
        router: MultiBookRouter,
        risk_engine: Optional[PreTradeRiskEngine] = None,
    ) -> Dict[str, Any]:
        """
        Restore router and risk_engine state from snapshot file.
        """
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        if data.get("magic") != self.SNAPSHOT_MAGIC:
            raise ValueError(f"Invalid snapshot file header in {filepath}")

        last_seq = data.get("last_seq", 0)
        router.order_id_counter = data.get("order_id_counter", 1)

        # Restore client order registry
        if "client_order_registry" in data:
            router.client_order_registry.clear()
            for k, v in data["client_order_registry"].items():
                router.client_order_registry[int(k)] = int(v)

        # Restore accounts
        if risk_engine is not None and "accounts" in data:
            risk_engine.accounts.clear()
            for acc_data in data["accounts"]:
                cid = acc_data["client_id"]
                acc = AccountBalance(
                    client_id=cid,
                    cash_balance=acc_data.get("cash_balance", 0.0),
                    cash_reserved=acc_data.get("cash_reserved", 0.0),
                    positions={int(k): int(v) for k, v in acc_data.get("positions", {}).items()},
                    positions_reserved={int(k): int(v) for k, v in acc_data.get("positions_reserved", {}).items()},
                    active_order_reservations={
                        int(k): v for k, v in acc_data.get("active_order_reservations", {}).items()
                    },
                )
                risk_engine.accounts[cid] = acc

        # Restore venues
        restored_venues = 0
        for vdata in data.get("venues", []):
            sym_id = vdata["symbol_id"]
            if sym_id in router.venues_by_id:
                venue = router.venues_by_id[sym_id]
            else:
                info = SymbolInfo(
                    symbol_id=sym_id,
                    ticker=vdata["ticker"],
                    base_asset=vdata["base_asset"],
                    quote_asset=vdata.get("quote_asset", "USD"),
                    tick_size=vdata.get("tick_size", 0.01),
                    lot_size=vdata.get("lot_size", 1),
                    initial_price=vdata.get("initial_price", 150.0),
                    price_scale=vdata.get("price_scale", 10_000),
                )
                venue = router.register_symbol(info, seed=False)

            # Restore metrics
            venue.total_orders = vdata.get("total_orders", 0)
            venue.total_trades = vdata.get("total_trades", 0)
            venue.total_volume = vdata.get("total_volume", 0)
            venue.last_trade_price = vdata.get("last_trade_price", venue.info.initial_price)
            venue.high_price = vdata.get("high_price", venue.info.initial_price)
            venue.low_price = vdata.get("low_price", venue.info.initial_price)
            venue.cumulative_notional = vdata.get("cumulative_notional", 0.0)

            # Re-insert resting orders
            venue.user_open_orders.clear()
            for o in vdata.get("user_open_orders", []):
                oid = o["order_id"]
                venue.user_open_orders[oid] = dict(o)

            restored_venues += 1

        return {
            "last_seq": last_seq,
            "order_id_counter": router.order_id_counter,
            "restored_venues": restored_venues,
            "restored_accounts": len(data.get("accounts", [])),
        }
