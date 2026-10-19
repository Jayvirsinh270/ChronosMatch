"""
ChronosMatch: Institutional Whale Sweep & Latency SLA Anomaly Detector.
Identifies aggressive multi-level order sweeps, large block executions,
and execution latency SLA breaches in real-time.
"""

from collections import deque
from dataclasses import dataclass
import time
from typing import Deque, Dict, List, Optional, Tuple, Any

from chronos_match.protocol import TradePacket, Side, from_fixed_price


@dataclass(frozen=True)
class WhaleAlert:
    """Represents a high-impact institutional execution or SLA breach event."""
    alert_id: int
    timestamp_ns: int
    trade_id: int
    buy_order_id: int
    sell_order_id: int
    symbol_id: int
    aggressor_side: str
    price: float
    quantity: int
    notional_usd: float
    latency_us: float
    alert_type: str  # "WHALE_SWEEP", "LARGE_BLOCK", "SLA_BREACH"
    details: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "timestamp_ns": self.timestamp_ns,
            "trade_id": self.trade_id,
            "buy_order_id": self.buy_order_id,
            "sell_order_id": self.sell_order_id,
            "symbol_id": self.symbol_id,
            "aggressor_side": self.aggressor_side,
            "price": self.price,
            "quantity": self.quantity,
            "notional_usd": self.notional_usd,
            "latency_us": self.latency_us,
            "alert_type": self.alert_type,
            "details": self.details,
        }


class WhaleDetector:
    """
    Real-time streaming detector analyzing trade streams for institutional flow and latency anomalies.
    """

    def __init__(
        self,
        volume_threshold: int = 5_000,
        notional_threshold: float = 250_000.0,
        sla_latency_threshold_ns: int = 50_000,  # 50 microsecond SLA target
        max_history: int = 200,
    ):
        self.volume_threshold = volume_threshold
        self.notional_threshold = notional_threshold
        self.sla_latency_threshold_ns = sla_latency_threshold_ns
        self.max_history = max_history

        self._alerts: Deque[WhaleAlert] = deque(maxlen=max_history)
        self._alert_counter = 0

        # State tracking for sweep detection (grouping fills with identical aggressor within 100 microseconds)
        self._last_aggressor_id: Optional[int] = None
        self._last_aggressor_side: Optional[int] = None
        self._last_aggressor_time_ns: int = 0
        self._sweep_fill_count: int = 0
        self._sweep_accumulated_shares: int = 0

        # Metrics counters
        self.total_alerts = 0
        self.whale_sweeps_count = 0
        self.large_blocks_count = 0
        self.sla_breaches_count = 0

    def inspect_trade(self, trade: TradePacket) -> Optional[WhaleAlert]:
        """
        Inspect an executed trade packet in real-time.
        Returns a WhaleAlert if a pattern or anomaly is detected, else None.
        """
        price_float = from_fixed_price(trade.price)
        notional = price_float * trade.quantity
        latency_us = trade.latency_ns / 1000.0
        aggressor_side_str = "BUY" if trade.aggressor_side == int(Side.BUY) else "SELL"
        aggressor_order_id = trade.buy_order_id if trade.aggressor_side == int(Side.BUY) else trade.sell_order_id

        alert: Optional[WhaleAlert] = None

        # 1. Check for Multi-fill Aggressive Sweep
        is_same_aggressor = (
            aggressor_order_id == self._last_aggressor_id
            and (trade.match_timestamp_ns - self._last_aggressor_time_ns) < 500_000  # within 500 μs
        )

        if is_same_aggressor:
            self._sweep_fill_count += 1
            self._sweep_accumulated_shares += trade.quantity
        else:
            self._last_aggressor_id = aggressor_order_id
            self._last_aggressor_side = trade.aggressor_side
            self._last_aggressor_time_ns = trade.match_timestamp_ns
            self._sweep_fill_count = 1
            self._sweep_accumulated_shares = trade.quantity

        # If sweep has crossed multiple price fills or accumulated substantial size
        if self._sweep_fill_count >= 3 or (
            self._sweep_fill_count >= 2 and self._sweep_accumulated_shares >= self.volume_threshold
        ):
            self._alert_counter += 1
            self.total_alerts += 1
            self.whale_sweeps_count += 1
            alert = WhaleAlert(
                alert_id=self._alert_counter,
                timestamp_ns=trade.match_timestamp_ns,
                trade_id=trade.trade_id,
                buy_order_id=trade.buy_order_id,
                sell_order_id=trade.sell_order_id,
                symbol_id=trade.symbol_id,
                aggressor_side=aggressor_side_str,
                price=price_float,
                quantity=self._sweep_accumulated_shares,
                notional_usd=self._sweep_accumulated_shares * price_float,
                latency_us=latency_us,
                alert_type="WHALE_SWEEP",
                details=f"Multi-Level Sweep: {self._sweep_fill_count} consecutive fills ({self._sweep_accumulated_shares:,} shares)",
            )

        # 2. Check for Single Large Block Institutional Trade
        elif trade.quantity >= self.volume_threshold or notional >= self.notional_threshold:
            self._alert_counter += 1
            self.total_alerts += 1
            self.large_blocks_count += 1
            alert = WhaleAlert(
                alert_id=self._alert_counter,
                timestamp_ns=trade.match_timestamp_ns,
                trade_id=trade.trade_id,
                buy_order_id=trade.buy_order_id,
                sell_order_id=trade.sell_order_id,
                symbol_id=trade.symbol_id,
                aggressor_side=aggressor_side_str,
                price=price_float,
                quantity=trade.quantity,
                notional_usd=notional,
                latency_us=latency_us,
                alert_type="LARGE_BLOCK",
                details=f"Block Execution: {trade.quantity:,} shares @ ${price_float:,.2f} (${notional:,.2f})",
            )

        # 3. Check for SLA Latency Breach (> 50 microseconds)
        elif trade.latency_ns > self.sla_latency_threshold_ns:
            self._alert_counter += 1
            self.total_alerts += 1
            self.sla_breaches_count += 1
            alert = WhaleAlert(
                alert_id=self._alert_counter,
                timestamp_ns=trade.match_timestamp_ns,
                trade_id=trade.trade_id,
                buy_order_id=trade.buy_order_id,
                sell_order_id=trade.sell_order_id,
                symbol_id=trade.symbol_id,
                aggressor_side=aggressor_side_str,
                price=price_float,
                quantity=trade.quantity,
                notional_usd=notional,
                latency_us=latency_us,
                alert_type="SLA_BREACH",
                details=f"SLA Breach: {latency_us:.2f} μs exceeded {self.sla_latency_threshold_ns / 1000.0:.1f} μs limit",
            )

        if alert is not None:
            self._alerts.append(alert)

        return alert

    def get_recent_alerts(self, limit: int = 10) -> List[WhaleAlert]:
        """Return the most recent alerts up to limit."""
        alerts = list(self._alerts)
        return alerts[-limit:] if len(alerts) > limit else alerts

    def get_statistics(self) -> Dict[str, Any]:
        """Return aggregated summary of detected market events."""
        return {
            "total_alerts": self.total_alerts,
            "whale_sweeps": self.whale_sweeps_count,
            "large_blocks": self.large_blocks_count,
            "sla_breaches": self.sla_breaches_count,
            "active_alerts_in_window": len(self._alerts),
        }
