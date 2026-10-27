# ChronosMatch ⚡
### Zero-Copy High-Frequency Trading (HFT) Matching Engine & Institutional Trading Terminal

[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.15-blue.svg)](https://www.python.org/)
[![Cython](https://img.shields.io/badge/Cython-3.3+-orange.svg)](https://cython.org/)
[![C](https://img.shields.io/badge/C-C11%2FMSVC-00599C.svg)](https://learn.microsoft.com/en-us/cpp/)
[![IPC](https://img.shields.io/badge/IPC-Zero--Copy%20SPSC-success.svg)](#2-zero-copy-shared-memory-bus-spsc)
[![Latency](https://img.shields.io/badge/Latency-Sub--50%CE%BCs-critical.svg)](#-empirical-performance-benchmarks)
[![Tests](https://img.shields.io/badge/Tests-173%2F173%20Passed-brightgreen.svg)](#-verification--test-suite-173173-passing)
[![Interface](https://img.shields.io/badge/Interface-Rich%20TUI%20%2B%20Web%20Cockpit-purple.svg)](#-how-to-start-tui--web-browser)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**ChronosMatch** is an ultra-low-latency, zero-copy financial matching engine and multi-venue institutional algorithmic trading platform. It overcomes the fundamental bottlenecks of traditional trading systems in Python: **Garbage Collection (GC) pauses**, **IPC serialization overhead (JSON/Pickle)**, and **Global Interpreter Lock (GIL) contention**.

Combining **hardware cache-line aligned shared memory (`mmap`)**, **Single-Producer Single-Consumer (SPSC) lock-free ring buffers**, and a **pure C/Cython Limit Order Book compiled with `nogil`**, ChronosMatch delivers deterministic **sub-50 microsecond execution times**, sustains over **490,000+ orders/sec** process-to-process IPC throughput, and offers both a **Bloomberg-style Rich Terminal TUI** and a modern **Dual-Mode Web Trading Cockpit** (Simple vs. Pro Mode).

---

## 🏛️ System Architecture

ChronosMatch isolates ingestion, order matching, multi-venue routing, persistence, and visual monitoring into dedicated OS processes communicating via bare-metal shared RAM:

```text
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        MARKET DATA FEEDS & INGESTION CLUSTER                           │
│   • Live Multi-Asset Tickers (NVDA, AAPL, MSFT, TSLA, BTC-USD, ETH-USD, SOL-USD)      │
│   • Real Market Feed Connectors (Alpha Vantage, Yahoo Finance, Synthetic Firehose)     │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │ Direct Binary 64B Structs (Zero Serialization)
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                   ZERO-COPY SHARED MEMORY BUS (mmap / Lock-Free SPSC)                  │
│       [Producer Head: 64B] ── [Consumer Tail: 64B] ── [Metadata Header: 64B]          │
│                  [64-Byte Cache-Line Aligned OrderPacket Slots]                        │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │ Direct Pointer Read (Zero-Copy)
                                            ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                     PURE C / CYTHON MATCHING ENGINE (nogil Core)                       │
│   • Price-Time Priority (FIFO) Limit Order Book (LOB)                                  │
│   • Fixed-Point Int64 Arithmetic (Zero Floating-Point Drift, 4-Decimal Scale)          │
│   • Pre-Allocated Static Memory Arena (Zero malloc/free in Hot Matching Path)          │
│   • Monotonic Nanosecond Latency Profiler (Hardware Ingress-to-Match Percentiles)      │
└──────────────┬────────────────────────────┬─────────────────────────────┬──────────────┘
               │                            │                             │
               ▼                            ▼                             ▼
┌────────────────────────────┐ ┌───────────────────────────┐ ┌───────────────────────────┐
│     MULTI-VENUE SOR &      │ │    QUANT & BACKTESTING    │ │    AI ALPHA RADAR & RL    │
│    HFT LATENCY ARBITRAGE   │ │   MONTE CARLO ENGINE      │ │     EXECUTION AGENT     │
│ • 4 Venues: NASDAQ, NYSE,  │ │ • Walk-Forward Backtest   │ │ • DQN Optimal Order Slicing│
│   BATS, IEX (350µs Bump)   │ │ • 1,000 Path Monte Carlo  │ │ • Market Regime Classifier │
│ • Consolidated NBBO Tape   │ │ • Sharpe / Sortino / MaxDD│ │ • Microsecond OFI Flow     │
│ • 4 SOR Smart Policies     │ │ • VaR 95/99% & CVaR       │ │ • Candlestick Pattern Rec  │
└──────────────┬─────────────┘ └────────────┬──────────────┘ └────────────┬──────────────┘
               │                            │                             │
               └────────────────────────────┼─────────────────────────────┘
                                            │
               ┌────────────────────────────┴────────────────────────────┐
               ▼                                                         ▼
┌──────────────────────────────────────────┐  ┌──────────────────────────────────────────┐
│   ASYNC SQLITE WAL AUDIT LEDGER WORKER   │  │       DUAL USER INTERFACES               │
│ • Non-blocking batched disk flush        │  │ • 🖥️  Bloomberg-Style Rich Terminal TUI  │
│ • Zero impact on critical hot path       │  │ • 🌐 Modern Web Browser Trading Cockpit  │
│ • Full trade & order book event replay   │  │   (Simple Mode & Pro Cockpit, Canvas)    │
└──────────────────────────────────────────┘  └──────────────────────────────────────────┘
```

---

## 🖥️ User Interfaces: Terminal TUI & Web Browser

ChronosMatch provides two first-class trading interfaces:

### 1. 🖥️ Bloomberg-Style Terminal TUI (Command Line)
* **Real-time ASCII Order Book Ladder**: Instant visualization of active Bids and Asks with volume depth bars.
* **Microsecond Latency Percentiles**: Live tracking of Min, p50, p90, p99, and p99.9 latency.
* **Whale Execution Alert Banner**: Immediate visual callout for orders larger than 5,000 shares.
* **Continuous Streaming or Soak Modes**: Benchmarked up to 500,000+ orders at maximum line speed.

### 2. 🌐 Modern Web Browser Trading Cockpit (FastAPI + Canvas)
* **Dual-Persona Experience**:
  * **Simple Mode (New / Retail Traders)**: Distraction-free native Canvas charts, quick cash percentage buttons (`10%`, `25%`, `50%`, `MAX`), 1-click mid-price execution, and beginner guidance banner.
  * **Pro Cockpit (Institutional Quants)**: Full Level 2 market depth ladder, live microsecond tape, whale alerts, multi-venue SOR desk, AI pattern radar, and Monte Carlo risk cones.
* **Zero External Dependencies**: 100% self-contained semantic HTML5, CSS Grid, and vanilla JavaScript Canvas. No CDNs, no heavy libraries (Bootstrap/Tailwind/Chart.js). Loads in **$<10\,\text{ms}$** and is completely offline-ready.
* **Integrated 7-Tab Workstation Dock**:
  1. 📋 **Open Orders & Execution Stream**: Live order manager with 1-click cancel and global `ESC` panic button.
  2. ⚡ **Live Tape & Whale Detector**: Real-time microsecond trade prints and institutional whale flags.
  3. 🤖 **Algo Desk & DQN RL Agent**: Control Avellaneda-Stoikov market making, Momentum Breakout, and DQN child order slicer.
  4. 🔮 **AI Pattern Radar**: Online Market Regime Classifier, Order Flow Imbalance (OFI), and Candlestick recognition.
  5. 📊 **Quant Tear Sheet & Monte Carlo**: Interactive backtest runner with Sharpe, Sortino, Calmar, MaxDD, and 1,000-path Monte Carlo simulations.
  6. 🌐 **Multi-Venue SOR & Latency Arbitrage**: Real-time consolidated NBBO SIP tape across NASDAQ, NYSE, BATS, IEX with crossed-market sniper and manual shock injector.
  7. 🛡️ **Risk & Margin**: Master circuit breaker halt button, portfolio margin, and cash reserve allocations.

---

## ⚡ How to Start: TUI & Web Browser

### 🚀 Option A: Launch Interactive Web Browser Terminal (GUI)

The web terminal launches a high-performance local server and opens your default web browser automatically:

```powershell
# Default launch (http://127.0.0.1:8000 with NVDA-USD)
python web_app.py

# Custom port and starting ticker
python web_app.py --port 8080 --symbol AAPL

# Headless server mode (without auto-opening browser)
python web_app.py --no-browser
```

Open your browser to: **`http://127.0.0.1:8000`**

---

### 🚀 Option B: Launch Live Bloomberg-Style Terminal TUI

Run the multi-process trading cluster directly in your terminal with high-frequency Rich dashboard rendering:

```powershell
# Interactive TUI with 100,000 simulated orders at 100k orders/sec
python main.py --orders 100000 --rate 100000

# Continuous streaming mode (runs indefinitely until Ctrl+C)
python main.py --orders 0

# Headless soak test benchmark (ideal for raw throughput stress-testing)
python main.py --no-ui --orders 500000
```

---

### 🚀 Option C: Run Executive Market Scenarios Demo

Experience automated market stress-tests (liquidity shocks, whale sweeps, algorithmic replenishment):

```powershell
python demo.py
```

---

## 🚀 Core Engineering Features

### 1. Bare-Metal Cython Matching Engine
* **GIL Bypassing (`nogil`)**: Matching loops execute free of the Python Global Interpreter Lock, eliminating GC pauses.
* **Fixed-Point Int64 Arithmetic**: Prices are scaled by $10^4$ ($100.25 \rightarrow 1,002,500$), preventing IEEE-754 floating-point inaccuracies.
* **Pre-Allocated Memory Arenas**: Pre-allocated pools for orders and price levels eliminate OS `malloc`/`free` calls in the hot path.
* **Hardware Latency Profiler**: Monotonic `QueryPerformanceCounter` timestamps record nanosecond-level ingress-to-match deltas.

### 2. Zero-Copy Shared-Memory Bus (SPSC)
* **Zero Serialization Overhead**: Orders are transmitted as raw 64-byte binary records. No JSON, Pickle, or socket copying.
* **False-Sharing Elimination**: Monotonic `head` (producer) and `tail` (consumer) cursors are separated onto dedicated 64-byte cache lines.
* **Bitwise Fast Modulo**: Power-of-2 ring capacities enable bitwise masking (`slot = index & (capacity - 1)`), bypassing CPU division.

### 3. Multi-Venue Smart Order Router (SOR) & Latency Arbitrage
* **Fragmented Venue Network**: Simulates 4 major US exchanges:
  * **NASDAQ**: 1.2 ms latency, maker rebate $-0.0020$/sh, taker fee $+0.0030$/sh.
  * **NYSE**: 1.5 ms latency, maker rebate $-0.0015$/sh, taker fee $+0.0028$/sh.
  * **BATS**: 0.9 ms latency, inverted fee maker rebate $+0.0020$/sh, taker fee $-0.0015$/sh.
  * **IEX**: 1.1 ms + $350\,\mu\text{s}$ speed bump, flat $0.0009$/sh fee.
* **Consolidated NBBO SIP Tape**: Computes real-time National Best Bid and Offer and instantly detects locked/crossed markets.
* **4 SOR Routing Policies**:
  * `NBBO_BEST_PRICE`: Directs order to venue displaying the single best price.
  * `PRO_RATA_DEPTH`: Distributes order across venues proportional to displayed L2 depth.
  * `NET_COST_OPTIMIZER`: Evaluates net price including exchange maker-taker fee schedules.
  * `SIMULTANEOUS_SWEEP`: Staggers order dispatch times to neutralize inter-exchange latency discrepancies.
* **Sub-Millisecond Latency Arbitrage Bot**: Scans crossed NBBO conditions and simultaneously snipes bid/ask legs for risk-free spread capture.

### 4. Institutional Strategy Backtesting & Monte Carlo Risk Engine
* **Walk-Forward Historical Backtesting**: Simulates realistic maker-taker fees, exchange slippage, and market impact.
* **Comprehensive Quant Tear Sheet**: Computes Annualized Return, Volatility, Sharpe Ratio, Sortino Ratio, Calmar Ratio, Maximum Drawdown, and Win Rate.
* **Monte Carlo Simulation**: 1,000 geometric Brownian motion paths projecting future portfolio equity distributions, Value at Risk (VaR 95%, VaR 99%), and Conditional Value at Risk (CVaR / Expected Shortfall).
* **Built-in Quant Strategies**:
  * Dual EMA Trend Breakout (12 / 26 EMA)
  * Bollinger Bands Mean Reversion
  * Bayesian Order Flow Momentum
  * TWAP/VWAP Institutional Benchmark

### 5. AI Alpha Radar & Reinforcement Learning (RL) Desk
* **Market Regime Classifier**: Real-time classification into `TRENDING_BULL`, `TRENDING_BEAR`, `RANGING`, or `HIGH_VOLATILITY`.
* **Microsecond Order Flow Imbalance (OFI)**: Measures aggressive buyer vs. seller pressure directly from Level 2 delta volumes.
* **Candlestick Pattern Recognition**: Fast automated detection of Bullish/Bearish Engulfing, Hammer, Shooting Star, and Star patterns.
* **DQN RL Optimal Execution Agent**: Deep Q-Network child order slicing agent optimizing execution against temporary and permanent market impact while penalizing inventory risk.

### 6. Decoupled Persistence & WAL Audit Ledger
* **Non-Blocking SQLite WAL Mode**: Trades and cancellations are batched and written asynchronously by a background persistence thread.
* **Event Replay**: Full audit trail allowing deterministic reconstruction of order book state at any historical microsecond.

---

## 📊 Empirical Performance Benchmarks

*Benchmarked on AMD64 / Windows 11 / Python 3.15 / MSVC v14.51 (`/O2` Release Build):*

| Metric | Target Specification | Achieved Benchmark |
| :--- | :--- | :--- |
| **Process-to-Process IPC** | $> 100,000\text{ orders/sec}$ | **492,609 orders/sec** |
| **IPC Batch Time (50k orders)** | $< 500\text{ ms}$ | **101.5 ms** |
| **Data Serialization Overhead** | Zero-Copy (0 bytes serialized) | **100% Raw Memory Mapped** |
| **Cache Line False Sharing** | 0% (Explicit 64B Padding) | **Validated (192B Header)** |
| **Hot Path Memory Allocation** | 0 allocations (`malloc` = 0) | **Pre-allocated Arena** |
| **Matching Engine Latency** | $< 50\text{ }\mu\text{s (p99)}$ | **Deterministic Sub-50 }\mu\text{s** |
| **Full Pytest Suite Verification** | 100% Pass Rate | **173 / 173 Tests Passing** |

---

## 📂 Project Repository Structure

```text
chronos_match/
├── chronos_match/                 # Core Python & Cython package
│   ├── __init__.py                # Package root exports
│   ├── protocol.py                # Binary 64B packing, fixed-point math, struct alignment
│   ├── ring_buffer.py             # Lock-free SPSC mmap ring buffer (cache-line padded)
│   ├── daemon.py                  # Multi-process orchestrator & lifecycle manager
│   │
│   ├── engine/                    # Ultra-low-latency matching core
│   │   ├── order_book_cy.pyx      # Pure C Limit Order Book (nogil, price-time priority)
│   │   └── ...                    # Cython build artifacts (.pyd/.c)
│   │
│   ├── market/                    # Real & simulated market data engine
│   │   ├── multi_asset_engine.py  # NVDA, AAPL, MSFT, TSLA, BTC, ETH, SOL feeds
│   │   ├── sor_network.py         # 4 fragmented exchanges (NASDAQ, NYSE, BATS, IEX)
│   │   └── nbbo.py                # Consolidated SIP tape & NBBO calculator
│   │
│   ├── algo/                      # Algorithmic & HFT trading execution
│   │   ├── smart_order_router.py  # 4 SOR policies (NBBO, Pro-Rata, Cost, Stagger)
│   │   ├── latency_arbitrage.py   # Sub-millisecond crossed-market arbitrage sniper
│   │   ├── dqn_agent.py           # Deep Q-Network reinforcement learning agent
│   │   ├── execution_env.py       # Almgren-Chriss optimal order slicing gym
│   │   ├── avellaneda_stoikov.py  # High-frequency market making bot
│   │   └── momentum_trader.py     # Volatility breakout trend bot
│   │
│   ├── quant/                     # Institutional quantitative analytics
│   │   ├── backtester.py          # Walk-forward event-driven backtesting engine
│   │   ├── monte_carlo.py         # 1,000-path geometric Brownian risk engine
│   │   └── metrics.py             # Sharpe, Sortino, Calmar, MaxDD, Win Rate
│   │
│   ├── ml/                        # AI & Microstructure feature engine
│   │   ├── regime_detector.py     # Real-time market regime classifier
│   │   ├── feature_store.py       # Microsecond Order Flow Imbalance (OFI)
│   │   └── candlestick_patterns.py# Automated technical candle pattern recognition
│   │
│   ├── web/                       # Modern institutional web trading platform
│   │   ├── server.py              # FastAPI + WebSocket asynchronous server
│   │   └── index.html             # Zero-dependency dual-mode canvas trading cockpit
│   │
│   ├── monitor/                   # Bloomberg-style terminal TUI
│   │   └── dashboard.py           # Real-time Rich terminal UI (30-60 FPS)
│   │
│   └── ledger/                    # Persistence & audit engine
│       └── audit_logger.py        # Asynchronous batched SQLite WAL logger
│
├── include/                       # Native C header files
│   ├── chronos_types.h            # 64-byte aligned OrderPacket, TradePacket, Header
│   └── arena.h                    # Static memory arena pools
│
├── tests/                         # Pytest test suite (173 tests)
│   ├── test_ring_buffer.py        # SPSC shared memory & concurrency tests
│   ├── test_engine.py             # Limit order book matching & FIFO tests
│   ├── test_web_server.py         # FastAPI REST & WebSocket endpoint tests
│   ├── test_smart_order_router.py # SOR policies & latency arbitrage tests
│   ├── test_backtester.py         # Quant backtesting & Monte Carlo tests
│   ├── test_dqn_execution.py      # RL agent & Gym environment tests
│   ├── test_ml_signals.py         # Regime detection & OFI feature tests
│   └── ...                        # Total 16 comprehensive test suites
│
├── web_app.py                     # Web Browser Trading Terminal launcher
├── main.py                        # Multi-Process Production & TUI entrypoint
├── demo.py                        # Automated market scenario demo runner
├── setup.py                       # Cython compilation script (MSVC /O2)
└── pytest.ini                     # Pytest configuration
```

---

## 🛠️ Installation & Build Setup

### 1. Prerequisites
* **Python 3.11+** (Developed & validated on Python 3.15)
* **C/C++ Compiler**: Microsoft Visual C++ Build Tools (MSVC `cl.exe`) on Windows, or GCC/Clang on Linux/macOS.

### 2. Environment Setup

```powershell
# Clone the repository
git clone https://github.com/Jayvirsinh270/ChronosMatch.git
cd ChronosMatch

# Create and activate virtual environment
python -m venv .venv

# Windows PowerShell:
.\.venv\Scripts\Activate.ps1

# Linux / macOS:
source .venv/bin/activate

# Install required dependencies
pip install cython setuptools rich pytest fastapi uvicorn websockets pydantic requests
```

### 3. Compile Cython C-Extensions

Compile the pure-C Limit Order Book with native MSVC `/O2` optimization:

```powershell
python setup.py build_ext --inplace
```

---

## 🧪 Verification & Test Suite (173/173 Passing)

ChronosMatch includes an exhaustive 16-suite test matrix covering memory arenas, SPSC ring buffers, order matching, SOR routing, latency arbitrage, ML signals, and REST/WebSocket endpoints:

```powershell
# Run the complete test suite
pytest -v

# Or run concise summary
pytest -q
```

Expected result:
```text
173 passed in 33.64s
```

---

## 🌐 Web API & WebSocket Endpoints

When running `python web_app.py`, the following REST & WebSocket endpoints are live:

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Serves the zero-dependency Dual-Mode Web Cockpit |
| `GET` | `/api/snapshot` | Returns L2 depth ladder, spread, best bid/ask, and recent trades |
| `POST` | `/api/order` | Place direct market or limit order into matching engine |
| `POST` | `/api/cancel` | Instant O(1) order cancellation |
| `GET` | `/api/sor/venues` | Query status, latencies, fees, and depth of all 4 exchanges |
| `GET` | `/api/sor/nbbo` | Consolidated National Best Bid & Offer (NBBO) quote |
| `POST` | `/api/sor/route-order` | Execute parent order via chosen SOR policy (NBBO, Pro-Rata, Cost, Sweep) |
| `POST` | `/api/sor/market-shock` | Inject latency order shock to induce crossed market arbitrage |
| `GET` | `/api/sor/arbitrage/stats` | Telemetry of autonomous sub-millisecond cross-venue arbitrage bot |
| `POST` | `/api/quant/backtest` | Run institutional backtest & 1,000-path Monte Carlo risk analysis |
| `POST` | `/api/risk/circuit-breaker/toggle` | Master emergency trading halt toggle |
| `WS` | `/ws` | High-frequency 16 FPS real-time L2 order book & tape stream |

---

## 👥 Authors & Maintainers

* **Project Owner / Lead Engineer**: **Jayvirsinh270** ([GitHub](https://github.com/Jayvirsinh270))

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
