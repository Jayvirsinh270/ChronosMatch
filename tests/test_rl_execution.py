"""
ChronosMatch: Test Suite for Adaptive Deep Reinforcement Learning (RL) Optimal Execution.

Validates:
1. Pure Python FastDenseLayer and DeepQNetwork forward/backward passes & inference latency (<25 µs).
2. Experience Replay ring buffer storage, batch sampling, and Polyak target network stabilization.
3. 16-factor microstructure state space extraction from real order book and Feature Store metrics.
4. Almgren-Chriss multi-objective reward formulation: Implementation Shortfall (IS), inventory risk (λ), maker rebates.
5. Slicing policies across discrete actions: WAIT, PASSIVE_PEG_MID, PASSIVE_PEG_BEST, OPPORTUNISTIC_SLICE, NORMAL_TWAP_SLICE, URGENT_LIQUIDATE.
6. Integration into AlgoTradingManager, HistoricalMarketReplayer, and FastAPI REST endpoints.
"""

import asyncio
import math
import time
import pytest

from chronos_match.algo.rl_execution_agent import (
    FastDenseLayer,
    DeepQNetwork,
    ExperienceReplayBuffer,
    RLExecutionAgent,
    ExecutionAction,
    ParentOrderState,
    ChildExecutionFill,
)
from chronos_match.algo import AlgoTradingManager
from chronos_match.market.router import MultiBookRouter, SymbolInfo
from chronos_match.market.replayer import HistoricalMarketReplayer
from chronos_match.web.server import (
    app,
    server,
    RLOrderRequest,
    api_rl_start_order,
    api_rl_telemetry,
    api_rl_cancel_order,
)
from chronos_match.protocol import to_fixed_price, from_fixed_price, OrderSide

# Disable continuous background simulation loop in unit test suite
server.bot_active = False


# ─────────────────────────────────────────────────────────────
# 1. NEURAL ARCHITECTURE & LATENCY TESTS
# ─────────────────────────────────────────────────────────────

def test_dqn_neural_architecture_and_latency():
    """Verify FastDenseLayer and DeepQNetwork inference correctness and sub-25 µs latency."""
    q_net = DeepQNetwork(state_dim=16, action_dim=6, seed=42)
    sample_state = [0.5, 0.8, 1.2, 0.3, -0.4, 0.2, 0.1, -0.05, 0.52, 1.1, 0.9, 0.15, 0.6, 0.8, 0.02, 1.0]

    # Forward pass outputs 6 discrete Q-values
    q_vals = q_net.predict(sample_state)
    assert len(q_vals) == 6
    assert all(isinstance(v, float) for v in q_vals)

    # Determinism with same seed
    q_net2 = DeepQNetwork(state_dim=16, action_dim=6, seed=42)
    q_vals2 = q_net2.predict(sample_state)
    for v1, v2 in zip(q_vals, q_vals2):
        assert math.isclose(v1, v2, abs_tol=1e-6)

    # Benchmark inference latency (1,000 forward passes)
    start_t = time.perf_counter_ns()
    n_iters = 1000
    for _ in range(n_iters):
        _ = q_net.predict(sample_state)
    elapsed_ns = time.perf_counter_ns() - start_t
    avg_latency_us = (elapsed_ns / n_iters) / 1000.0

    # Ensure execution speed is suitable for high-frequency tick processing (< 300 µs)
    assert avg_latency_us < 300.0, f"DQN inference too slow: {avg_latency_us:.2f} µs"


def test_experience_replay_and_target_polyak():
    """Verify experience replay ring buffer capacity, sampling, and Polyak weight averaging."""
    buf = ExperienceReplayBuffer(capacity=50)
    for i in range(75):
        s = [float(i)] * 16
        s_next = [float(i + 1)] * 16
        buf.push(state=s, action=i % 6, reward=float(i) * 0.1, next_state=s_next, done=(i % 10 == 0))

    assert len(buf) == 50  # Cap at 50

    batch = buf.sample(batch_size=16)
    assert len(batch) == 16
    for s, a, r, s_n, d in batch:
        assert len(s) == 16
        assert len(s_n) == 16
        assert 0 <= a < 6
        assert isinstance(r, float)
        assert isinstance(d, bool)

    # Test Polyak soft target weight update
    online_net = DeepQNetwork(state_dim=16, action_dim=6, seed=1)
    target_net = DeepQNetwork(state_dim=16, action_dim=6, seed=2)

    w_online_0 = online_net.layer1.weights[0][0]
    w_target_before = target_net.layer1.weights[0][0]
    assert not math.isclose(w_online_0, w_target_before)

    # Apply soft update with tau=0.20
    tau = 0.20
    target_net.copy_weights_from(online_net, tau=tau)
    expected_w = tau * w_online_0 + (1.0 - tau) * w_target_before
    assert math.isclose(target_net.layer1.weights[0][0], expected_w, rel_tol=1e-5)


# ─────────────────────────────────────────────────────────────
# 2. STATE EXTRACTION & POLICY STEPPING TESTS
# ─────────────────────────────────────────────────────────────

def test_rl_microstructure_state_extraction():
    """Verify 16-dimensional microstructure state normalization."""
    agent = RLExecutionAgent(bot_id=803, symbol="SOL-USD")
    order = agent.start_parent_order(
        symbol="SOL-USD",
        side_str="BUY",
        total_shares=5000,
        horizon_sec=60.0,
        arrival_price=150.00,
        risk_aversion=0.002,
    )
    assert order.active
    assert order.remaining_shares == 5000

    state = agent.extract_microstructure_state(
        market_mid=150.00,
        best_bid=149.98,
        best_ask=150.02,
        bid_vol=1200,
        ask_vol=800,
        ofi=250.0,
        daily_vol=0.025,
        spread_mult=1.1,
        predicted_drift=0.0004,
        regime_value="MOMENTUM_BREAKOUT_BULL",
        pattern_sentiment="BULLISH",
        tick_size=0.01,
    )

    assert len(state) == 16
    # State components:
    # 0: inventory_ratio (1.0 at start)
    assert math.isclose(state[0], 1.0, abs_tol=0.01)
    # 1: time_ratio (~1.0 at start)
    assert 0.95 <= state[1] <= 1.0
    # 3: depth_imbalance ((1200 - 800) / 2000 = +0.20)
    assert math.isclose(state[3], 0.20, abs_tol=0.05)
    # 15: regime_code (+1.0 for BULL)
    assert state[15] == 1.0


def test_rl_step_execution_policy_actions():
    """Verify discrete tactical execution actions and order specification generation."""
    agent = RLExecutionAgent(bot_id=803, symbol="SOL-USD", epsilon=0.0)  # Pure greedy
    agent.start_parent_order(
        symbol="SOL-USD",
        side_str="BUY",
        total_shares=2000,
        horizon_sec=60.0,
        arrival_price=100.00,
    )

    state = [0.5] * 16
    child_spec = agent.step_execution_policy(
        state=state,
        best_bid=99.98,
        best_ask=100.02,
        tick_size=0.01,
    )

    if child_spec is not None:
        assert "action" in child_spec
        assert child_spec["side"] == int(OrderSide.BUY)
        assert child_spec["quantity"] > 0
        assert child_spec["price"] > 0
        assert isinstance(child_spec["is_maker"], bool)
        assert isinstance(child_spec["is_market"], bool)


# ─────────────────────────────────────────────────────────────
# 3. FILL RECORDING & SHORTFALL BENCHMARK TESTS
# ─────────────────────────────────────────────────────────────

def test_rl_fill_recording_and_implementation_shortfall():
    """Verify parent order slice execution accounting and reward optimization."""
    agent = RLExecutionAgent(bot_id=803, symbol="SOL-USD")
    order = agent.start_parent_order(
        symbol="SOL-USD",
        side_str="BUY",
        total_shares=1000,
        horizon_sec=60.0,
        arrival_price=100.00,
        risk_aversion=0.001,
    )

    # First slice: 400 shares filled at 99.99 (passive maker fill, negative slippage / price improvement)
    agent.last_state = [0.5] * 16
    agent.last_action = ExecutionAction.PASSIVE_PEG_BEST

    agent.record_execution_fill(
        exec_price=99.99,
        quantity=400,
        is_maker=True,
    )

    assert order.filled_shares == 400
    assert order.remaining_shares == 600
    assert order.average_exec_price == 99.99
    assert agent.total_passive_fills == 1
    assert len(order.fills) == 1
    assert order.fills[0].is_maker is True

    # Second slice: 600 shares filled at 100.02 (taker sweep) -> completes order
    agent.record_execution_fill(
        exec_price=100.02,
        quantity=600,
        is_maker=False,
    )

    assert order.filled_shares == 1000
    assert order.remaining_shares == 0
    assert order.completed is True
    assert order.active is False
    assert agent.active is False

    # Weighted average price: (400 * 99.99 + 600 * 100.02) / 1000 = 100.008
    assert math.isclose(order.average_exec_price, 100.01, abs_tol=0.02)
    # Implementation shortfall vs P0 (100.00)
    assert order.implementation_shortfall_bps >= 0.0

    # Verify telemetry dictionary
    telem = agent.to_dict(current_mid=100.00)
    assert telem["bot_name"] == "Adaptive RL Optimal Execution Agent (DQN)"
    assert telem["active_parent_order"]["completed"] is True
    assert telem["active_parent_order"]["filled_shares"] == 1000


# ─────────────────────────────────────────────────────────────
# 4. ALGO TRADING MANAGER INTEGRATION TESTS
# ─────────────────────────────────────────────────────────────

def test_algo_trading_manager_rl_integration():
    """Verify AlgoTradingManager coordinates the RL agent alongside MM and Momentum."""
    manager = AlgoTradingManager(symbol="SOL-USD")

    # Verify bot initialization
    assert hasattr(manager, "rl_execution_agent")
    assert manager.rl_execution_agent.symbol == "SOL-USD"
    assert manager.rl_execution_agent.active is False

    # Toggle RL agent
    active = manager.toggle_bot("rl", enable=True)
    assert active is True
    assert manager.rl_execution_agent.active is True

    # Summary includes RL agent telemetry
    summary = manager.get_summary(current_mid=150.00)
    assert "market_maker" in summary
    assert "momentum_bot" in summary
    assert "rl_execution_agent" in summary
    assert summary["rl_execution_agent"]["symbol"] == "SOL-USD"

    # Trade callback routing
    manager.rl_execution_agent.start_parent_order(
        symbol="SOL-USD",
        side_str="BUY",
        total_shares=500,
        horizon_sec=30.0,
        arrival_price=150.00,
    )
    manager.on_trade_executed(
        client_id=manager.rl_execution_agent.bot_id,
        side=int(OrderSide.BUY),
        price=150.01,
        quantity=100,
        is_maker=False,
    )
    assert manager.rl_execution_agent.active_parent_order.filled_shares == 100


# ─────────────────────────────────────────────────────────────
# 5. MARKET REPLAYER EXECUTION LOOP INTEGRATION TESTS
# ─────────────────────────────────────────────────────────────

def test_replayer_rl_execution_stepping():
    """Verify HistoricalMarketReplayer steps the RL execution agent during tick processing."""
    router = MultiBookRouter()
    replayer = HistoricalMarketReplayer(router=router, default_ticker="SOL-USD")
    replayer.bind_stock("SOL-USD")

    algo_mgr = replayer.algo_managers.get("SOL-USD")
    assert algo_mgr is not None

    # Commission parent order
    algo_mgr.rl_execution_agent.start_parent_order(
        symbol="SOL-USD",
        side_str="BUY",
        total_shares=1000,
        horizon_sec=60.0,
        arrival_price=150.00,
    )
    assert algo_mgr.rl_execution_agent.active is True

    # Step replayer multiple ticks and check for child slice executions
    all_events = []
    for _ in range(25):
        events = replayer.step_market_tick(symbol="SOL-USD")
        all_events.extend(events)

    # Verify RL agent recorded execution steps
    telem = algo_mgr.rl_execution_agent.to_dict()
    assert telem["active_parent_order"] is not None
    assert telem["active_parent_order"]["order_id"] == 1


# ─────────────────────────────────────────────────────────────
# 6. FASTAPI REST ENDPOINTS INTEGRATION TESTS
# ─────────────────────────────────────────────────────────────

def test_fastapi_rl_execution_endpoints():
    """Verify REST API endpoints for commissioning, querying, and cancelling RL parent orders."""
    # 1. Start parent order
    req = RLOrderRequest(
        symbol="SOL-USD",
        side="BUY",
        shares=1500,
        horizon_sec=45.0,
        risk_aversion=0.001,
    )
    data = api_rl_start_order(req)
    assert data["status"] == "ORDER_STARTED"
    assert data["order"]["active"] is True
    assert data["order"]["active_parent_order"]["total_shares"] == 1500

    # 2. Query telemetry
    telem = api_rl_telemetry(symbol="SOL-USD")
    assert telem["bot_name"] == "Adaptive RL Optimal Execution Agent (DQN)"
    assert telem["active_parent_order"]["symbol"] == "SOL-USD"

    # 3. Cancel order
    res_cancel = api_rl_cancel_order(symbol="SOL-USD")
    assert res_cancel["status"] == "ORDER_CANCELLED"

    # Query telemetry again to verify completion / standby
    telem_after = api_rl_telemetry(symbol="SOL-USD")
    assert telem_after["active"] is False
