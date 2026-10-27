"""
ChronosMatch: National Best Bid and Offer (NBBO) Consolidated Tape Engine.

Aggregates real-time top-of-book and multi-level depth quotes across all fragmented
exchange venues. Detects locked markets (NBB == NBO) and crossed markets (NBB > NBO)
signaling cross-exchange arbitrage opportunities.
"""

from dataclasses import dataclass, field
import time
from typing import Dict, List, Optional, Tuple, Any

from chronos_match.market.sor_network import MultiVenueNetwork, ExchangeVenue


@dataclass
class VenueQuote:
    """Top-of-book quote snapshot for a single exchange venue."""
    venue_id: str
    bid_price: float
    bid_size: int
    ask_price: float
    ask_size: int
    spread: float
    timestamp_ns: int


@dataclass
class NBBORecord:
    """Consolidated National Best Bid and Offer (NBBO) market snapshot."""
    symbol: str
    timestamp_ns: int
    national_best_bid: float
    national_best_bid_size: int
    national_best_bid_venue: str
    national_best_bid_venues: List[str] # All venues matching best bid
    national_best_offer: float
    national_best_offer_size: int
    national_best_offer_venue: str
    national_best_offer_venues: List[str] # All venues matching best offer
    spread: float
    spread_bps: float
    is_locked: bool              # True if NBB == NBO
    is_crossed: bool             # True if NBB > NBO (Cross-Exchange Arbitrage Window!)
    arbitrage_spread: float      # Positive dollar amount if crossed (NBB - NBO)
    venue_quotes: Dict[str, Dict[str, Any]]
    consolidated_bids: List[Dict[str, Any]] = field(default_factory=list)
    consolidated_asks: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp_ns": self.timestamp_ns,
            "national_best_bid": round(self.national_best_bid, 2),
            "national_best_bid_size": self.national_best_bid_size,
            "national_best_bid_venue": self.national_best_bid_venue,
            "national_best_bid_venues": self.national_best_bid_venues,
            "national_best_offer": round(self.national_best_offer, 2),
            "national_best_offer_size": self.national_best_offer_size,
            "national_best_offer_venue": self.national_best_offer_venue,
            "national_best_offer_venues": self.national_best_offer_venues,
            "spread": round(self.spread, 2),
            "spread_bps": round(self.spread_bps, 2),
            "is_locked": self.is_locked,
            "is_crossed": self.is_crossed,
            "arbitrage_spread": round(self.arbitrage_spread, 2),
            "venue_quotes": self.venue_quotes,
            "consolidated_bids": self.consolidated_bids[:5],
            "consolidated_asks": self.consolidated_asks[:5],
        }


class NBBOEngine:
    """
    Consolidated Tape Association (CTA) / Unlisted Trading Privileges (UTP) SIP
    simulation engine producing unified National Best Bid & Offer.
    """

    def __init__(self, network: MultiVenueNetwork):
        self.network = network
        self.history: Dict[str, List[NBBORecord]] = {}

    def compute_nbbo(self, symbol: str) -> NBBORecord:
        """
        Evaluate all venues in the network, identify national best bid/offer,
        and assess cross-market locked/crossed conditions.
        """
        sym = symbol.upper()
        now_ns = time.time_ns()

        venue_quotes: Dict[str, Dict[str, Any]] = {}
        all_bids: List[Tuple[float, int, str]] = []  # (price, size, venue_id)
        all_asks: List[Tuple[float, int, str]] = []

        for vid, venue in self.network.venues.items():
            tob = venue.get_top_of_book(sym)
            venue_quotes[vid] = tob

            if tob["best_bid"] > 0:
                all_bids.append((tob["best_bid"], tob["bid_size"], vid))
            if tob["best_ask"] > 0:
                all_asks.append((tob["best_ask"], tob["ask_size"], vid))

        # 1. Determine National Best Bid (Highest Bid Across All Venues)
        if all_bids:
            all_bids.sort(key=lambda x: x[0], reverse=True)
            nbb_price = all_bids[0][0]
            # Aggregate volume across all venues posting this exact price
            nbb_venues = [b[2] for b in all_bids if abs(b[0] - nbb_price) < 1e-4]
            nbb_size = sum(b[1] for b in all_bids if abs(b[0] - nbb_price) < 1e-4)
            nbb_lead_venue = nbb_venues[0]
        else:
            nbb_price = 0.0
            nbb_size = 0
            nbb_venues = []
            nbb_lead_venue = "NONE"

        # 2. Determine National Best Offer (Lowest Ask Across All Venues)
        if all_asks:
            all_asks.sort(key=lambda x: x[0])
            nbo_price = all_asks[0][0]
            nbo_venues = [a[2] for a in all_asks if abs(a[0] - nbo_price) < 1e-4]
            nbo_size = sum(a[1] for a in all_asks if abs(a[0] - nbo_price) < 1e-4)
            nbo_lead_venue = nbo_venues[0]
        else:
            nbo_price = 0.0
            nbo_size = 0
            nbo_venues = []
            nbo_lead_venue = "NONE"

        # 3. Calculate Consolidated Spread and Locked / Crossed Condition
        spread = round(nbo_price - nbb_price, 2) if (nbo_price > 0 and nbb_price > 0) else 0.0
        spread_bps = round((spread / nbb_price) * 10000.0, 1) if nbb_price > 0 else 0.0

        is_locked = (abs(spread) < 1e-4 and nbb_price > 0 and nbo_price > 0)
        is_crossed = (nbb_price > nbo_price and nbo_price > 0 and nbb_price > 0)
        arb_spread = round(nbb_price - nbo_price, 2) if is_crossed else 0.0

        # 4. Aggregate Consolidated Depth Book
        consolidated_bids = self._aggregate_depth(all_bids, reverse=True)
        consolidated_asks = self._aggregate_depth(all_asks, reverse=False)

        record = NBBORecord(
            symbol=sym,
            timestamp_ns=now_ns,
            national_best_bid=nbb_price,
            national_best_bid_size=nbb_size,
            national_best_bid_venue=nbb_lead_venue,
            national_best_bid_venues=nbb_venues,
            national_best_offer=nbo_price,
            national_best_offer_size=nbo_size,
            national_best_offer_venue=nbo_lead_venue,
            national_best_offer_venues=nbo_venues,
            spread=spread,
            spread_bps=spread_bps,
            is_locked=is_locked,
            is_crossed=is_crossed,
            arbitrage_spread=arb_spread,
            venue_quotes=venue_quotes,
            consolidated_bids=consolidated_bids,
            consolidated_asks=consolidated_asks,
        )

        if sym not in self.history:
            self.history[sym] = []
        self.history[sym].append(record)
        if len(self.history[sym]) > 500:
            self.history[sym].pop(0)

        return record

    def _aggregate_depth(self, quotes: List[Tuple[float, int, str]], reverse: bool) -> List[Dict[str, Any]]:
        """Group quotes by price level and list participating venues."""
        price_map: Dict[float, Dict[str, Any]] = {}
        for price, size, vid in quotes:
            p_rounded = round(price, 2)
            if p_rounded not in price_map:
                price_map[p_rounded] = {"price": p_rounded, "size": 0, "venues": []}
            price_map[p_rounded]["size"] += size
            price_map[p_rounded]["venues"].append(vid)

        res = list(price_map.values())
        res.sort(key=lambda x: x["price"], reverse=reverse)
        return res
