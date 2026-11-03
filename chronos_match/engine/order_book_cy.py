"""
ChronosMatch: Order Book Engine (Hybrid Cython + Pure Python Fallback).
Loads high-performance C/Cython extension if compiled for the host OS/arch,
or seamlessly falls back to pure Python for serverless (Vercel / AWS Lambda / Linux).
"""

import os
import sys
import time
from typing import Dict, List, Optional, Any

_FastOrderBook_C = None

# Attempt to load compiled native C-extension if available on the current OS
try:
    _dir = os.path.dirname(os.path.abspath(__file__))
    for fname in os.listdir(_dir):
        if fname.startswith("order_book_cy.") and (fname.endswith(".pyd") or fname.endswith(".so")):
            try:
                import importlib.machinery
                import importlib.util
                loader = importlib.machinery.ExtensionFileLoader("chronos_match.engine.order_book_cy", os.path.join(_dir, fname))
                spec = importlib.util.spec_from_loader("chronos_match.engine.order_book_cy", loader)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "FastOrderBook"):
                    _FastOrderBook_C = mod.FastOrderBook
                    break
            except Exception:
                pass
except Exception:
    pass


class _PyFastOrderBook:
    """
    Pure-Python high-fidelity matching engine implementation of FastOrderBook.
    Provides identical Price-Time Priority order matching and Level 2 depth for serverless environments.
    """

    def __init__(self, arena_capacity: int = 100000):
        self.arena_capacity = arena_capacity
        # order_id -> dict
        self._orders: Dict[int, Dict[str, Any]] = {}
        # price -> list of order_ids in time order
        self._bids: Dict[int, List[int]] = {}
        self._asks: Dict[int, List[int]] = {}
        self.total_trades_matched = 0
        self.total_shares_traded = 0
        self._trade_id_seq = 1

    @property
    def best_bid_price(self) -> int:
        return max(self._bids.keys()) if self._bids else 0

    @property
    def best_bid_volume(self) -> int:
        bb = self.best_bid_price
        if bb <= 0:
            return 0
        return sum(self._orders[oid]["quantity"] for oid in self._bids[bb])

    @property
    def best_ask_price(self) -> int:
        return min(self._asks.keys()) if self._asks else 0

    @property
    def best_ask_volume(self) -> int:
        ba = self.best_ask_price
        if ba <= 0:
            return 0
        return sum(self._orders[oid]["quantity"] for oid in self._asks[ba])

    @property
    def spread(self) -> int:
        bb = self.best_bid_price
        ba = self.best_ask_price
        if bb > 0 and ba > 0 and ba > bb:
            return ba - bb
        return 0

    def cancel_order(self, order_id: int) -> bool:
        """O(1) cancellation of active limit order."""
        order = self._orders.pop(order_id, None)
        if not order:
            return False
        price = order["price"]
        side = order["side"]
        book = self._bids if side == 0 else self._asks
        if price in book:
            if order_id in book[price]:
                book[price].remove(order_id)
            if not book[price]:
                del book[price]
        return True

    def cull_stale(self, max_bid: int, min_ask: int) -> int:
        """Cull out-of-market orders."""
        culled = 0
        # Cull bids above max_bid
        bid_prices = list(self._bids.keys())
        for p in bid_prices:
            if max_bid > 0 and p > max_bid:
                for oid in list(self._bids[p]):
                    self._orders.pop(oid, None)
                    culled += 1
                del self._bids[p]

        # Cull asks below min_ask
        ask_prices = list(self._asks.keys())
        for p in ask_prices:
            if min_ask > 0 and p < min_ask:
                for oid in list(self._asks[p]):
                    self._orders.pop(oid, None)
                    culled += 1
                del self._asks[p]

        return culled

    def get_l2_snapshot(self, depth: int = 10) -> dict:
        """Return top N bids and asks."""
        bids_out = []
        for p in sorted(self._bids.keys(), reverse=True)[:depth]:
            vol = sum(self._orders[oid]["quantity"] for oid in self._bids[p])
            bids_out.append({"price": p, "volume": vol, "order_count": len(self._bids[p])})

        asks_out = []
        for p in sorted(self._asks.keys())[:depth]:
            vol = sum(self._orders[oid]["quantity"] for oid in self._asks[p])
            asks_out.append({"price": p, "volume": vol, "order_count": len(self._asks[p])})

        return {"bids": bids_out, "asks": asks_out}

    def add_limit_order(
        self,
        order_id: int,
        client_id: int,
        price: int,
        quantity: int,
        side: int,
        symbol_id: int = 1,
        timestamp_ns: int = 0,
    ) -> list:
        """Process incoming limit order against order book."""
        if timestamp_ns == 0:
            timestamp_ns = time.perf_counter_ns()

        rem_qty = quantity
        matches = []

        # Side 0 = BUY, Side 1 = SELL
        if side == 0:
            # Match against lowest resting Asks <= price
            while rem_qty > 0 and self._asks:
                best_ask = min(self._asks.keys())
                if best_ask > price:
                    break

                ask_queue = self._asks[best_ask]
                while rem_qty > 0 and ask_queue:
                    maker_id = ask_queue[0]
                    maker = self._orders[maker_id]
                    trade_qty = min(rem_qty, maker["quantity"])

                    trade_event = {
                        "trade_id": self._trade_id_seq,
                        "buy_order_id": order_id,
                        "sell_order_id": maker_id,
                        "execution_price": best_ask,
                        "quantity": trade_qty,
                        "aggressor_side": 0,
                        "match_timestamp_ns": timestamp_ns,
                    }
                    self._trade_id_seq += 1
                    self.total_trades_matched += 1
                    self.total_shares_traded += trade_qty
                    matches.append(trade_event)

                    rem_qty -= trade_qty
                    maker["quantity"] -= trade_qty

                    if maker["quantity"] <= 0:
                        ask_queue.pop(0)
                        del self._orders[maker_id]

                if not ask_queue:
                    del self._asks[best_ask]

            # If leftover quantity, rest in Bid book
            if rem_qty > 0:
                self._orders[order_id] = {
                    "order_id": order_id,
                    "client_id": client_id,
                    "price": price,
                    "quantity": rem_qty,
                    "side": 0,
                    "timestamp_ns": timestamp_ns,
                }
                self._bids.setdefault(price, []).append(order_id)

        else:
            # Match against highest resting Bids >= price
            while rem_qty > 0 and self._bids:
                best_bid = max(self._bids.keys())
                if best_bid < price:
                    break

                bid_queue = self._bids[best_bid]
                while rem_qty > 0 and bid_queue:
                    maker_id = bid_queue[0]
                    maker = self._orders[maker_id]
                    trade_qty = min(rem_qty, maker["quantity"])

                    trade_event = {
                        "trade_id": self._trade_id_seq,
                        "buy_order_id": maker_id,
                        "sell_order_id": order_id,
                        "execution_price": best_bid,
                        "quantity": trade_qty,
                        "aggressor_side": 1,
                        "match_timestamp_ns": timestamp_ns,
                    }
                    self._trade_id_seq += 1
                    self.total_trades_matched += 1
                    self.total_shares_traded += trade_qty
                    matches.append(trade_event)

                    rem_qty -= trade_qty
                    maker["quantity"] -= trade_qty

                    if maker["quantity"] <= 0:
                        bid_queue.pop(0)
                        del self._orders[maker_id]

                if not bid_queue:
                    del self._bids[best_bid]

            # If leftover quantity, rest in Ask book
            if rem_qty > 0:
                self._orders[order_id] = {
                    "order_id": order_id,
                    "client_id": client_id,
                    "price": price,
                    "quantity": rem_qty,
                    "side": 1,
                    "timestamp_ns": timestamp_ns,
                }
                self._asks.setdefault(price, []).append(order_id)

        return matches


# Export FastOrderBook class (C-native if available, otherwise pure Python)
FastOrderBook = _FastOrderBook_C if _FastOrderBook_C is not None else _PyFastOrderBook
