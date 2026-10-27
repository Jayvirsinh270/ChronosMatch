"""
Unit and integration test suite for ChronosMatch Multi-Exchange Smart Order Router (SOR),
Consolidated NBBO Engine, and High-Frequency Cross-Venue Latency Arbitrage.
"""

import pytest

from chronos_match.market.sor_network import (
    MultiVenueNetwork,
    ExchangeVenue,
    VenueConfig,
    VenueFill,
)
from chronos_match.market.nbbo import (
    NBBOEngine,
    NBBORecord,
)
from chronos_match.algo.smart_order_router import (
    SmartOrderRouter,
    RoutingPolicy,
    SORRoutingResult,
)
from chronos_match.algo.latency_arbitrage import (
    LatencyArbitrageEngine,
    ArbitrageOpportunity,
)
from chronos_match.web.server import (
    SORRouteRequest,
    MarketShockRequest,
    api_get_sor_venues,
    api_get_sor_nbbo,
    api_get_arbitrage_stats,
    api_toggle_arbitrage,
    api_step_arbitrage,
    api_route_smart_order,
    api_inject_market_shock,
)


def test_multi_venue_network_creation_and_latency_matrix():
    """Verify multi-exchange datacenter topology and speed-of-light latency matrix."""
    network = MultiVenueNetwork()
    assert len(network.venues) >= 4

    # Verify standard institutional venues
    nasdaq = network.get_venue("NASDAQ")
    nyse = network.get_venue("NYSE")
    bats = network.get_venue("BATS")
    iex = network.get_venue("IEX")

    assert nasdaq.config.mic_code == "XNAS"
    assert nyse.config.mic_code == "XNYS"
    assert bats.config.mic_code == "BATS"
    assert iex.config.speed_bump_ms > 0.0  # IEX 350 microsecond speed bump coil

    # Verify pairwise latencies
    lat_nasdaq_nyse = network.get_pairwise_latency_ms("NASDAQ", "NYSE")
    lat_nasdaq_bats = network.get_pairwise_latency_ms("NASDAQ", "BATS")
    assert lat_nasdaq_nyse > 0.0
    assert lat_nasdaq_bats > 0.0

    # Seed liquidity
    network.seed_network(symbol="NVDA", mid_price=130.00, spread_bps=5.0)
    for v in network.venues.values():
        tob = v.get_top_of_book("NVDA")
        assert tob["best_bid"] > 0
        assert tob["best_ask"] > 0
        assert tob["best_ask"] > tob["best_bid"]


def test_nbbo_engine_aggregation_locked_and_crossed():
    """Verify SIP Consolidated Tape Association NBBO computation and cross-market arbitrage detection."""
    network = MultiVenueNetwork()
    nbbo_engine = NBBOEngine(network)

    # Seed baseline quotes
    network.seed_network(symbol="AAPL", mid_price=220.00, spread_bps=4.0)

    # 1. Standard regular market
    nbbo = nbbo_engine.compute_nbbo("AAPL")
    assert isinstance(nbbo, NBBORecord)
    assert nbbo.national_best_bid > 0
    assert nbbo.national_best_offer > 0
    assert nbbo.national_best_bid <= nbbo.national_best_offer
    assert not nbbo.is_crossed

    # 2. Simulate crossed market dislocation
    # Force BATS to have an aggressive resting Bid at $222.00, while NYSE has Ask at $220.10
    bats = network.get_venue("BATS")
    bats.execute_limit_order("AAPL", side="BUY", price=222.00, quantity=10000)

    nbbo_crossed = nbbo_engine.compute_nbbo("AAPL")
    assert nbbo_crossed.is_crossed
    assert nbbo_crossed.arbitrage_spread > 0.0
    assert nbbo_crossed.national_best_bid_venue == "BATS"

    d = nbbo_crossed.to_dict()
    assert d["is_crossed"] is True
    assert "venue_quotes" in d


def test_sor_routing_nbbo_best_execution():
    """Verify Smart Order Router NBBO price sweep."""
    network = MultiVenueNetwork()
    network.seed_network(symbol="MSFT", mid_price=400.00, spread_bps=5.0)
    sor = SmartOrderRouter(network)

    # Route BUY order for 600 shares using NBBO Best Execution
    result = sor.route_order(
        symbol="MSFT",
        side="BUY",
        quantity=600,
        policy=RoutingPolicy.NBBO_BEST_PRICE,
    )
    assert isinstance(result, SORRoutingResult)
    assert result.filled_quantity == 600
    assert result.average_fill_price > 0.0
    assert len(result.venue_allocations) >= 1
    assert result.execution_time_ms >= 0.0


def test_sor_routing_pro_rata_depth_split():
    """Verify Smart Order Router pro-rata volume allocation based on visible depth."""
    network = MultiVenueNetwork()
    network.seed_network(symbol="NVDA", mid_price=130.00, spread_bps=6.0)
    sor = SmartOrderRouter(network)

    result = sor.route_order(
        symbol="NVDA",
        side="BUY",
        quantity=2000,
        policy=RoutingPolicy.PRO_RATA_DEPTH,
    )
    assert result.filled_quantity > 0
    # Pro-rata should split across multiple venues
    assert len(result.venue_allocations) >= 2


def test_sor_routing_cost_and_fee_optimization():
    """Verify cost-optimized routing factoring in exchange taker fees and maker rebates."""
    network = MultiVenueNetwork()
    network.seed_network(symbol="TSLA", mid_price=250.00, spread_bps=5.0)
    sor = SmartOrderRouter(network)

    result = sor.route_order(
        symbol="TSLA",
        side="BUY",
        quantity=800,
        policy=RoutingPolicy.COST_AND_FEE_OPTIMIZED,
    )
    assert result.filled_quantity == 800
    assert result.total_fees_usd > 0.0
    assert result.effective_all_in_price > result.average_fill_price  # Buyer effective price includes fees


def test_sor_routing_simultaneous_latency_stagger():
    """Verify speed-of-light concurrent arrival stagger calculation."""
    network = MultiVenueNetwork()
    network.seed_network(symbol="NVDA", mid_price=130.00, spread_bps=5.0)
    sor = SmartOrderRouter(network)

    result = sor.route_order(
        symbol="NVDA",
        side="SELL",
        quantity=1500,
        policy=RoutingPolicy.SIMULTANEOUS_SWEEP,
    )
    assert result.filled_quantity > 0
    # Inspect child orders dispatch delays
    delays = [c["dispatch_delay_ms"] for c in result.child_orders]
    assert len(delays) > 0
    # Staggered sweep ensures at least one venue has non-zero delay relative to the slowest
    assert min(delays) == 0.0  # Slowest venue fires immediately


def test_hft_latency_arbitrage_detection_and_snipe():
    """Verify real-time cross-exchange arbitrage detection and sub-millisecond execution."""
    network = MultiVenueNetwork()
    network.seed_network(symbol="NVDA", mid_price=130.00, spread_bps=4.0)
    arb_engine = LatencyArbitrageEngine(network)

    # 1. Before shock: no crossed markets
    opps_before = arb_engine.scan_opportunities("NVDA")
    assert len(opps_before) == 0

    # 2. Inject order flow shock on BATS: buy with high price
    network.inject_market_shock("NVDA", venue_id="BATS", price_delta=0.60)

    # 3. Detect arbitrage
    opps = arb_engine.scan_opportunities("NVDA")
    assert len(opps) > 0
    opp = opps[0]
    assert opp.spread_usd > 0.0
    assert opp.gross_profit_usd > 0.0

    # 4. Snipe the opportunity
    success = arb_engine.execute_snipe(opp)
    assert success is True
    assert opp.status == "SNIPED"
    assert opp.net_profit_usd > 0.0
    assert arb_engine.total_net_profit > 0.0

    # 5. Check statistics
    stats = arb_engine.get_statistics()
    assert stats["total_snipes_executed"] >= 1
    assert stats["total_net_profit_usd"] > 0.0


def test_fastapi_sor_and_arbitrage_endpoints():
    """Verify REST API gateway handlers for Smart Order Routing and Arbitrage."""
    # 1. GET /api/sor/venues
    venues = api_get_sor_venues()
    assert isinstance(venues, list)
    assert len(venues) >= 4

    # 2. GET /api/sor/nbbo
    nbbo = api_get_sor_nbbo("NVDA")
    assert "national_best_bid" in nbbo
    assert "national_best_offer" in nbbo
    assert "spread" in nbbo

    # 3. POST /api/sor/route-order
    route_req = SORRouteRequest(
        symbol="NVDA",
        side="BUY",
        quantity=900,
        policy="NBBO_BEST_PRICE",
    )
    route_res = api_route_smart_order(route_req)
    assert route_res["filled_quantity"] == 900
    assert "venue_allocations" in route_res
    assert "average_fill_price" in route_res

    # 4. POST /api/sor/market-shock and POST /api/sor/arbitrage/step
    shock_req = MarketShockRequest(symbol="NVDA", venue_id="BATS", price_delta=0.50)
    shock_res = api_inject_market_shock(shock_req)
    assert shock_res["status"] == "SHOCK_INJECTED"

    step_res = api_step_arbitrage("NVDA")
    assert "sniped" in step_res
    assert "stats" in step_res

    # 5. POST /api/sor/arbitrage/toggle and GET /api/sor/arbitrage/stats
    toggle_res = api_toggle_arbitrage()
    assert "active" in toggle_res
    stats_res = api_get_arbitrage_stats()
    assert "total_snipes_executed" in stats_res
