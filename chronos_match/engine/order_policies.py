"""
ChronosMatch: Advanced Order Type Policy Engine.

Implements institutional-grade order execution modifiers as a pure-Python
policy layer that wraps the existing C-level FastOrderBook matching core.

Supported Features:
  - IOC  (Immediate-or-Cancel):  Fill what's available; cancel remainder.
  - FOK  (Fill-or-Kill):         Full fill or reject entirely.
  - POST_ONLY / Maker-Only:      Reject if order would cross spread and take liquidity.
  - STP  (Self-Trade Prevention): Cancel aggressor when both sides share client_id.
"""

from enum import IntEnum
from typing import Any, Dict, List, Optional, Tuple


class TimeInForce(IntEnum):
    """
    Time-In-Force execution modifiers.

    GTC  — Good-Till-Cancelled: Rests on book indefinitely (default legacy behaviour).
    IOC  — Immediate-or-Cancel: Fill available quantity; cancel any unfilled remainder.
    FOK  — Fill-or-Kill:        Entire quantity must fill immediately or the order is rejected.
    """
    GTC = 0
    IOC = 1
    FOK = 2


class SelfTradeAction(IntEnum):
    """
    Configurable Self-Trade Prevention action taken when both sides share client_id.

    ALLOW           — No STP check (legacy default).
    CANCEL_AGGRESSOR— Cancel the incoming aggressor; keep the resting order on the book.
    CANCEL_RESTING  — Cancel the resting order; let the aggressor rest instead.
    CANCEL_BOTH     — Cancel both sides immediately.
    """
    ALLOW            = 0
    CANCEL_AGGRESSOR = 1
    CANCEL_RESTING   = 2
    CANCEL_BOTH      = 3


class OrderPolicyResult:
    """
    Outcome of running an order through the PolicyEngine.
    Returned by OrderPolicyEngine.apply() and stored in API responses.
    """

    def __init__(
        self,
        accepted: bool,
        order_id: int,
        tif: TimeInForce,
        post_only: bool,
        stp_action: SelfTradeAction,
        matches: List[Dict[str, Any]],
        cancelled_remainder: int = 0,
        reject_reason: Optional[str] = None,
        stp_cancelled_qty: int = 0,
    ):
        self.accepted          = accepted          # False if FOK/POST_ONLY rejected before engine
        self.order_id          = order_id
        self.tif               = tif
        self.post_only         = post_only
        self.stp_action        = stp_action
        self.matches           = matches           # Raw match events from C engine
        self.cancelled_remainder = cancelled_remainder  # IOC: qty that was cancelled after partial fill
        self.reject_reason     = reject_reason     # Human-readable rejection string
        self.stp_cancelled_qty = stp_cancelled_qty # Shares voided by STP

    @property
    def total_filled(self) -> int:
        return sum(m["quantity"] for m in self.matches)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted":            self.accepted,
            "order_id":            self.order_id,
            "tif":                 self.tif.name,
            "post_only":           self.post_only,
            "stp_action":          self.stp_action.name,
            "total_filled":        self.total_filled,
            "cancelled_remainder": self.cancelled_remainder,
            "reject_reason":       self.reject_reason,
            "stp_cancelled_qty":   self.stp_cancelled_qty,
            "matched_trades":      self.matches,
        }


class OrderPolicyEngine:
    """
    Pre/post-processing policy layer sitting between the API and the C matching engine.

    Responsibilities:
      1. POST_ONLY check  — before sending to engine.
      2. FOK liquidity check  — before sending to engine (peek best available qty).
      3. STP filter  — filters out self-trade match events post-match.
      4. IOC/FOK cleanup  — cancels remainder via book.cancel_order() after match.

    The underlying FastOrderBook reference is injected at construction time so
    the engine holds a single shared book instance.
    """

    def __init__(self, book, client_order_registry: Optional[Dict[int, int]] = None):
        """
        Args:
            book:                   FastOrderBook instance (Cython-backed C engine).
            client_order_registry:  Dict mapping order_id -> client_id for STP checks.
                                    If None, STP cannot distinguish clients and is skipped.
        """
        self._book = book
        # order_id -> client_id mapping maintained externally (by WebTradingServer)
        self._client_registry: Dict[int, int] = client_order_registry if client_order_registry is not None else {}

    def register_order(self, order_id: int, client_id: int) -> None:
        """Track which client owns a given order_id for STP purposes."""
        self._client_registry[order_id] = client_id

    def deregister_order(self, order_id: int) -> None:
        """Remove an order from the registry when it is cancelled or fully filled."""
        self._client_registry.pop(order_id, None)

    def apply(
        self,
        order_id: int,
        client_id: int,
        price_fixed: int,           # Fixed-point price (already converted)
        quantity: int,
        side: int,                  # 0=BUY, 1=SELL
        symbol_id: int,
        timestamp_ns: int,
        tif: TimeInForce = TimeInForce.GTC,
        post_only: bool = False,
        stp_action: SelfTradeAction = SelfTradeAction.CANCEL_AGGRESSOR,
    ) -> OrderPolicyResult:
        """
        Execute an order with all policy constraints applied.

        Execution flow:
          [1] Post-Only guard   → reject before engine if price would cross spread.
          [2] FOK liquidity check → reject before engine if available qty < requested.
          [3] Register order in STP registry (for resting orders).
          [4] Send to C matching engine.
          [5] STP filter — remove any self-trade match events and cancel those legs.
          [6] IOC / FOK cleanup — cancel any unfilled remainder.

        Returns:
            OrderPolicyResult with full execution outcome.
        """
        from chronos_match.protocol import from_fixed_price

        # ── [1] POST-ONLY GUARD ─────────────────────────────────────────────
        if post_only:
            best_bid_px = self._book.best_bid_price
            best_ask_px = self._book.best_ask_price

            if side == 0:  # BUY: post-only rejected if price >= best ask (would take)
                if best_ask_px > 0 and price_fixed >= best_ask_px:
                    return OrderPolicyResult(
                        accepted=False,
                        order_id=order_id,
                        tif=tif,
                        post_only=post_only,
                        stp_action=stp_action,
                        matches=[],
                        reject_reason="POST_ONLY_REJECTED: Buy price would cross spread and take liquidity",
                    )
            else:  # SELL: post-only rejected if price <= best bid (would take)
                if best_bid_px > 0 and price_fixed <= best_bid_px:
                    return OrderPolicyResult(
                        accepted=False,
                        order_id=order_id,
                        tif=tif,
                        post_only=post_only,
                        stp_action=stp_action,
                        matches=[],
                        reject_reason="POST_ONLY_REJECTED: Sell price would cross spread and take liquidity",
                    )

        # ── [2] FOK PRE-CHECK: Verify sufficient available qty before touching engine ──
        if tif == TimeInForce.FOK:
            available = self._estimate_available_qty(side, price_fixed, quantity)
            if available < quantity:
                return OrderPolicyResult(
                    accepted=False,
                    order_id=order_id,
                    tif=tif,
                    post_only=post_only,
                    stp_action=stp_action,
                    matches=[],
                    reject_reason=f"FOK_REJECTED: Insufficient liquidity — needed {quantity:,} shares, "
                                  f"only {available:,} available at or better than limit price",
                )

        # ── [3] Register order in STP client registry (for resting legs) ────
        self.register_order(order_id, client_id)

        # ── [4] Submit to C matching engine ─────────────────────────────────
        raw_matches: List[Dict[str, Any]] = self._book.add_limit_order(
            order_id=order_id,
            client_id=client_id,
            price=price_fixed,
            quantity=quantity,
            side=side,
            symbol_id=symbol_id,
            timestamp_ns=timestamp_ns,
        )

        # ── [5] SELF-TRADE PREVENTION filter ─────────────────────────────────
        clean_matches, stp_cancelled_qty = self._apply_stp(
            client_id=client_id,
            matches=raw_matches,
            stp_action=stp_action,
        )

        filled_qty = sum(m["quantity"] for m in clean_matches)

        # ── [6] IOC / FOK POST-MATCH CLEANUP ─────────────────────────────────
        cancelled_remainder = 0

        if tif == TimeInForce.IOC:
            # IOC: cancel any unfilled remainder that rested on the book
            remainder = quantity - filled_qty - stp_cancelled_qty
            if remainder > 0:
                self._book.cancel_order(order_id)
                self.deregister_order(order_id)
                cancelled_remainder = remainder

        elif tif == TimeInForce.FOK:
            # FOK: if somehow we got here with a partial fill (shouldn't happen due to
            # pre-check, but guard defensively), cancel and reject the partial fills
            if filled_qty < quantity:
                self._book.cancel_order(order_id)
                self.deregister_order(order_id)
                return OrderPolicyResult(
                    accepted=False,
                    order_id=order_id,
                    tif=tif,
                    post_only=post_only,
                    stp_action=stp_action,
                    matches=[],
                    cancelled_remainder=quantity,
                    reject_reason="FOK_REJECTED: Partial fill detected at match stage — order voided",
                )

        # Deregister fully filled orders from the STP registry
        if filled_qty >= quantity:
            self.deregister_order(order_id)

        return OrderPolicyResult(
            accepted=True,
            order_id=order_id,
            tif=tif,
            post_only=post_only,
            stp_action=stp_action,
            matches=clean_matches,
            cancelled_remainder=cancelled_remainder,
            stp_cancelled_qty=stp_cancelled_qty,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _estimate_available_qty(self, side: int, price_fixed: int, needed: int) -> int:
        """
        Peek at the opposing side of the book to count how many shares are
        available at-or-better than the submitted price. Used for FOK pre-check.

        This reads from the C engine's Level-2 snapshot (no heap allocation in hot path).
        """
        snapshot = self._book.get_l2_snapshot(depth=32)

        if side == 0:  # BUY aggressor: check resting ASKS
            levels = snapshot.get("asks", [])
            available = 0
            for level in levels:
                if level["price"] <= price_fixed:  # Ask at or below our bid limit
                    available += level["volume"]
                    if available >= needed:
                        return available
            return available
        else:  # SELL aggressor: check resting BIDS
            levels = snapshot.get("bids", [])
            available = 0
            for level in levels:
                if level["price"] >= price_fixed:  # Bid at or above our ask limit
                    available += level["volume"]
                    if available >= needed:
                        return available
            return available

    def _apply_stp(
        self,
        client_id: int,
        matches: List[Dict[str, Any]],
        stp_action: SelfTradeAction,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Filter match events for self-trades. Returns (clean_matches, stp_cancelled_qty).

        A self-trade is detected when the resting counterpart's order_id maps to
        the same client_id as the aggressor in self._client_registry.
        """
        if stp_action == SelfTradeAction.ALLOW or not self._client_registry:
            return matches, 0

        clean_matches = []
        stp_cancelled_qty = 0

        for m in matches:
            # Identify the resting (passive) order id
            # aggressor_side 0 = BUY aggressor → sell_order_id is the resting leg
            # aggressor_side 1 = SELL aggressor → buy_order_id is the resting leg
            aggressor_side = m.get("aggressor_side", -1)
            resting_order_id = (
                m["sell_order_id"] if aggressor_side == 0 else m["buy_order_id"]
            )

            resting_client = self._client_registry.get(resting_order_id)

            if resting_client is not None and resting_client == client_id:
                # Self-trade detected
                stp_cancelled_qty += m["quantity"]

                if stp_action == SelfTradeAction.CANCEL_AGGRESSOR:
                    # Aggressor (this order) is rejected for this leg; resting order stays
                    # The resting leg should still be live on the book
                    pass  # Simply don't append to clean_matches

                elif stp_action == SelfTradeAction.CANCEL_RESTING:
                    # Cancel the resting order from the book; aggressor proceeds to next level
                    self._book.cancel_order(resting_order_id)
                    self.deregister_order(resting_order_id)
                    # The aggressor leg can try to match next level (re-route not done here;
                    # we simply skip this match and let the remainder rest or cancel via TIF)

                elif stp_action == SelfTradeAction.CANCEL_BOTH:
                    # Cancel the resting leg; also mark the aggressor leg for cancellation
                    self._book.cancel_order(resting_order_id)
                    self.deregister_order(resting_order_id)
                    # Don't append; aggressor leg will be cleaned up by TIF policy
            else:
                clean_matches.append(m)

        return clean_matches, stp_cancelled_qty
