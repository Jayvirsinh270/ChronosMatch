"""
ChronosMatch: High-Frequency Cross-Venue Latency Arbitrage Engine.

Monitors fragmented exchange books for inter-market crossed conditions
(Bid_A > Ask_B). Employs sub-millisecond execution to simultaneously
buy on the lagging venue and sell on the leading venue, capturing risk-free
microsecond statistical and latency arbitrage profits.
"""

from dataclasses import dataclass, field
import time
from typing import Dict, List, Optional, Tuple, Any

from chronos_match.market.sor_network import MultiVenueNetwork, ExchangeVenue, VenueFill
from chronos_match.market.nbbo import NBBOEngine, NBBORecord


@dataclass
class ArbitrageOpportunity:
    """Detected cross-exchange latency arbitrage window."""
    opportunity_id: int
    timestamp_ns: int
    symbol: str
    buy_venue: str           # The lagging/cheaper exchange with low Ask
    buy_price: float
    buy_size: int
    sell_venue: str          # The expensive exchange with high Bid
    sell_price: float
    sell_size: int
    spread_usd: float        # Dislocation: sell_price - buy_price
    spread_bps: float
    executable_quantity: int # min(buy_size, sell_size)
    gross_profit_usd: float
    estimated_fees_usd: float
    net_profit_usd: float
    latency_window_us: float # Microsecond race window before quote cancellation
    status: str = "DETECTED" # "DETECTED", "SNIPED", "FADED", "REJECTED"
    buy_fill: Optional[VenueFill] = None
    sell_fill: Optional[VenueFill] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "timestamp_ns": self.timestamp_ns,
            "symbol": self.symbol,
            "buy_venue": self.buy_venue,
            "buy_price": round(self.buy_price, 2),
            "buy_size": self.buy_size,
            "sell_venue": self.sell_venue,
            "sell_price": round(self.sell_price, 2),
            "sell_size": self.sell_size,
            "spread_usd": round(self.spread_usd, 2),
            "spread_bps": round(self.spread_bps, 2),
            "executable_quantity": self.executable_quantity,
            "gross_profit_usd": round(self.gross_profit_usd, 2),
            "estimated_fees_usd": round(self.estimated_fees_usd, 4),
            "net_profit_usd": round(self.net_profit_usd, 2),
            "latency_window_us": round(self.latency_window_us, 1),
            "status": self.status,
        }


class LatencyArbitrageEngine:
    """
    Sub-millisecond latency arbitrage scanner & sniper.
    """

    def __init__(self, network: MultiVenueNetwork, nbbo_engine: Optional[NBBOEngine] = None):
        self.network = network
        self.nbbo_engine = nbbo_engine or NBBOEngine(network)
        self.active: bool = True
        self.opportunity_counter = 1
        self.detected_opportunities: List[ArbitrageOpportunity] = []
        self.executed_snipes: List[ArbitrageOpportunity] = []
        self.total_gross_profit: float = 0.0
        self.total_net_profit: float = 0.0
        self.total_volume_sniped: int = 0

    def scan_opportunities(self, symbol: str) -> List[ArbitrageOpportunity]:
        """
        Scan all pairwise venue combinations for crossed markets (Bid_Venue_A > Ask_Venue_B).
        """
        sym = symbol.upper()
        now_ns = time.time_ns()
        venues = list(self.network.venues.values())
        opps: List[ArbitrageOpportunity] = []

        # Get top of book from every exchange
        quotes: Dict[str, Dict[str, Any]] = {}
        for v in venues:
            quotes[v.venue_id] = v.get_top_of_book(sym)

        # Cross-compare all venue pairs (O(N^2) where N is 4-8 venues, < 5 microseconds)
        for i in range(len(venues)):
            for j in range(len(venues)):
                if i == j:
                    continue
                v_sell = venues[i] # Expensive venue to sell on
                v_buy = venues[j]  # Cheaper venue to buy on

                tob_sell = quotes[v_sell.venue_id]
                tob_buy = quotes[v_buy.venue_id]

                best_bid = tob_sell["best_bid"]
                best_ask = tob_buy["best_ask"]

                # Check if crossed: Bid on venue A > Ask on venue B
                if best_bid > 0 and best_ask > 0 and best_bid > best_ask:
                    spread = best_bid - best_ask
                    spread_bps = (spread / best_ask) * 10000.0

                    exec_qty = min(tob_sell["bid_size"], tob_buy["ask_size"])
                    if exec_qty <= 0:
                        continue

                    gross_profit = spread * exec_qty
                    buy_fee = (best_ask * exec_qty) * (tob_buy["taker_fee_bps"] / 10000.0)
                    sell_fee = (best_bid * exec_qty) * (tob_sell["taker_fee_bps"] / 10000.0)
                    tot_fees = buy_fee + sell_fee
                    net_profit = gross_profit - tot_fees

                    # Estimate propagation window (speed of light between the two datacenters)
                    pairwise_ms = self.network.get_pairwise_latency_ms(v_buy.venue_id, v_sell.venue_id)
                    latency_window_us = pairwise_ms * 1000.0

                    opp = ArbitrageOpportunity(
                        opportunity_id=self._next_opp_id(),
                        timestamp_ns=now_ns,
                        symbol=sym,
                        buy_venue=v_buy.venue_id,
                        buy_price=best_ask,
                        buy_size=tob_buy["ask_size"],
                        sell_venue=v_sell.venue_id,
                        sell_price=best_bid,
                        sell_size=tob_sell["bid_size"],
                        spread_usd=spread,
                        spread_bps=spread_bps,
                        executable_quantity=exec_qty,
                        gross_profit_usd=gross_profit,
                        estimated_fees_usd=tot_fees,
                        net_profit_usd=net_profit,
                        latency_window_us=latency_window_us,
                        status="DETECTED",
                    )
                    opps.append(opp)
                    self.detected_opportunities.append(opp)
                    if len(self.detected_opportunities) > 200:
                        self.detected_opportunities.pop(0)

        return opps

    def execute_snipe(self, opp: ArbitrageOpportunity) -> bool:
        """
        Execute sub-millisecond simultaneous leg execution:
        Leg 1: Buy on buy_venue at buy_price.
        Leg 2: Sell on sell_venue at sell_price.
        """
        if not self.active:
            opp.status = "REJECTED_INACTIVE"
            return False

        v_buy = self.network.get_venue(opp.buy_venue)
        v_sell = self.network.get_venue(opp.sell_venue)

        # Execute both legs
        buy_fills, _ = v_buy.execute_limit_order(opp.symbol, side="BUY", price=opp.buy_price, quantity=opp.executable_quantity)
        sell_fills, _ = v_sell.execute_limit_order(opp.symbol, side="SELL", price=opp.sell_price, quantity=opp.executable_quantity)

        bought_qty = sum(f.quantity for f in buy_fills)
        sold_qty = sum(f.quantity for f in sell_fills)
        matched_qty = min(bought_qty, sold_qty)

        if matched_qty > 0:
            avg_buy_px = (sum(f.notional for f in buy_fills) / bought_qty) if bought_qty > 0 else opp.buy_price
            avg_sell_px = (sum(f.notional for f in sell_fills) / sold_qty) if sold_qty > 0 else opp.sell_price

            gross = (avg_sell_px - avg_buy_px) * matched_qty
            buy_fees = sum(f.fee_usd for f in buy_fills)
            sell_fees = sum(f.fee_usd for f in sell_fills)
            net = gross - (buy_fees + sell_fees)

            opp.gross_profit_usd = gross
            opp.estimated_fees_usd = buy_fees + sell_fees
            opp.net_profit_usd = net
            opp.status = "SNIPED"
            opp.buy_fill = buy_fills[0] if buy_fills else None
            opp.sell_fill = sell_fills[0] if sell_fills else None

            self.total_gross_profit += gross
            self.total_net_profit += net
            self.total_volume_sniped += matched_qty
            self.executed_snipes.append(opp)
            if len(self.executed_snipes) > 100:
                self.executed_snipes.pop(0)
            return True
        else:
            opp.status = "FADED"
            return False

    def step_arbitrage_cycle(self, symbol: str) -> Optional[ArbitrageOpportunity]:
        """Perform one online scan-and-snipe tick."""
        if not self.active:
            return None
        opps = self.scan_opportunities(symbol)
        profitable_opps = [o for o in opps if o.net_profit_usd > 0.50]
        if profitable_opps:
            # Pick highest net profit opportunity
            profitable_opps.sort(key=lambda x: x.net_profit_usd, reverse=True)
            target = profitable_opps[0]
            self.execute_snipe(target)
            return target
        return None

    def get_statistics(self) -> Dict[str, Any]:
        """Return operational telemetry of the HFT arbitrage desk."""
        return {
            "active": self.active,
            "total_snipes_executed": len(self.executed_snipes),
            "total_opportunities_detected": len(self.detected_opportunities),
            "total_gross_profit_usd": round(self.total_gross_profit, 2),
            "total_net_profit_usd": round(self.total_net_profit, 2),
            "total_volume_sniped": self.total_volume_sniped,
            "average_profit_per_snipe": round(self.total_net_profit / len(self.executed_snipes), 2) if self.executed_snipes else 0.0,
            "recent_snipes": [s.to_dict() for s in self.executed_snipes[-15:]],
        }

    def _next_opp_id(self) -> int:
        oid = self.opportunity_counter
        self.opportunity_counter += 1
        return oid
