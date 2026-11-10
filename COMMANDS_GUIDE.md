# ⚡ FluxCore — Complete CLI & Command Execution Guide

This document contains every runnable command in **FluxCore**, explaining:
- **Exact Command Syntax** (PowerShell / Windows / Linux)
- **What it Does (Under the Hood)**
- **How to Interpret and Verify the Results**
- **How to Buy & Sell Specific Company Stocks (NVDA, AAPL, MSFT, BTC, etc.)**

---

## 📑 Table of Contents
1. [Environment Setup & Build Commands](#1-environment-setup--build-commands)
2. [Terminal TUI & High-Frequency Ingestion (`main.py`)](#2-terminal-tui--high-frequency-ingestion-mainpy)
3. [Interactive Web Trading Cockpit (`web_app.py`)](#3-interactive-web-trading-cockpit-webapppy)
4. [Live Stock Order Placement Commands (REST / CLI / Python)](#4-live-stock-order-placement-commands-rest--cli--python)
5. [Executive Showcase & Market Stress Scenarios (`demo.py`)](#5-executive-showcase--market-stress-scenarios-demopy)
6. [Data Ingestion & Machine Learning Scripts](#6-data-ingestion--machine-learning-scripts)
7. [Comprehensive Verification & Test Suite (`pytest`)](#7-comprehensive-verification--test-suite-pytest)
8. [Quick-Reference Cheat Sheet](#8-quick-reference-cheat-sheet)

---

## 1. 🛠️ Environment Setup & Build Commands

Before running any benchmarks or trading engines, the native C/Cython extensions must be compiled.

### 1.1 Activate Python Virtual Environment
```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# Linux / macOS
source .venv/bin/activate
```
* **What it does**: Directs your terminal to use project dependencies (FastAPI, Cython, Rich, Pytest).
* **Expected Result**: Your terminal prompt shows `(.venv)`.

---

### 1.2 Compile Pure-C & Cython Limit Order Book
```powershell
python setup.py build_ext --inplace
```
* **What it does**: Compiles `fluxcore/engine/order_book_cy.pyx` into an optimized native C extension (`.pyd` on Windows, `.so` on Linux) using MSVC with `/O2` release optimizations.
* **Expected Result**:
  * You will see compiler output from `cl.exe`.
  * Generates `order_book_cy.cp315-win_amd64.pyd` (or your Python version equivalent) inside `fluxcore/engine/`.

---

### 1.3 Clean Build Artifacts (Reset Build)
```powershell
Remove-Item -Recurse -Force build, *.pyd, fluxcore\engine\*.pyd, fluxcore\engine\*.c -ErrorAction SilentlyContinue
```
* **What it does**: Deletes old compiled binaries and C code before doing a fresh compilation.

---

### 1.4 Master Systems Verification & Certification Audit (`verify_system.py`)
```powershell
# Fast verification (25,000 orders)
python verify_system.py --fast

# Full soak verification (100,000 orders)
python verify_system.py
```
* **What it does**:
  1. Audits compiled native MSVC C/Cython `.pyd` extensions (`order_book_cy`, `arena_cy`, `price_level_cy`).
  2. Executes unit tests across all 5 architectural phases.
  3. Runs single-thread matching soak benchmark computing wire-to-match latency percentiles ($p50, p90, p99, p99.9$).
  4. Generates the official **FluxCore Systems Engineering Certificate**.
* **Expected Result**: Single-thread throughput > 450,000 ord/s, $p99 < 50\mu s$ SLA met.

---

## 2. 🖥️ Terminal TUI & High-Frequency Ingestion (`main.py`)

`main.py` is the **multi-process exchange stress-test engine**. It spins up 4 independent OS processes:
1. `Market Firehose`: Generates orders at up to 100k+ orders/sec.
2. `Matching Engine`: Consumes orders via zero-copy shared RAM and executes price-time matching in Cython `nogil`.
3. `Audit Ledger`: Persists trades to SQLite WAL off the critical path.
4. `Rich Terminal TUI`: Visualizes live market depth, microsecond latency percentiles, and whale alerts.

---

### 2.1 Standard Live Benchmark with TUI (100,000 Orders)
```powershell
python main.py --orders 100000 --rate 100000
```
* **What it does**: Generates 100,000 synthetic market/limit orders at an ingestion rate of 100,000 orders/sec while displaying the live 30–60 FPS Rich dashboard.
* **What you see on screen**:
  * **Top Banner**: Status `NOMINAL`, zero-copy IPC health.
  * **KPI Cards**: Process-to-process throughput (orders/sec), order book imbalance, matched liquidity.
  * **Level 2 Depth Ladder**: Real-time ASCII ladder showing top 10 Bids (green) and top 10 Asks (red) with volume depth bars.
  * **Trade Execution Ticker**: Live trade prints with nanosecond latency metrics.
  * **Whale Alert Banner**: Alerts highlighted in yellow/red whenever an order exceeding 5,000 shares is executed.
  * **Exit Summary**: When complete, prints an audit report showing total duration, effective throughput, and memory stats.

---

### 2.2 Continuous Streaming Mode (Runs Indefinitely)
```powershell
python main.py --orders 0
```
* **What it does**: Runs the exchange matching engine and TUI continuously without stopping. Ideal for live demonstrations during presentations.
* **How to stop it**: Press `Ctrl + C`. The engine triggers a graceful shutdown, closes worker processes, flushes SQLite WAL, and unlinks shared memory.

---

### 2.3 Headless Soak Test (Raw Maximum Throughput Benchmark)
```powershell
python main.py --no-ui --orders 500000
```
* **What it does**: Disables terminal screen rendering so the CPU can dedicate 100% of cores to raw order matching and IPC throughput.
* **Expected Result**:
  * Runs 500,000 orders in approximately 1 second.
  * Prints final throughput statistics: `~490,000+ orders/sec`.

---

### 2.4 Custom Ingestion Rate & Custom Ring Buffer Capacity
```powershell
python main.py --orders 200000 --rate 50000 --capacity 131072 --db my_audit.db
```
* **Flags explained**:
  * `--orders 200000`: Match 200k orders.
  * `--rate 50000`: Ingest at 50,000 orders/sec.
  * `--capacity 131072`: Allocates a larger shared memory ring buffer (must be a power of 2).
  * `--db my_audit.db`: Saves audit trail to a custom SQLite database file.

---

### 2.5 Standalone Interactive TUI Simulator (`--demo`)
```powershell
python main.py --demo
# or equivalently:
python -m fluxcore.monitor.dashboard
```
* **What it does**: Launches the newly upgraded institutional-grade Rich Terminal UI directly in 20 FPS simulation mode without spawning multi-process IPC daemons.
* **What you see on screen**:
  * **Pulsing Header**: Architecture telemetry, live time with millisecond precision, and dynamic heartbeat indicator.
  * **Microstructure Sparklines**: Real-time Unicode rolling sparklines for trade prices (` ▂▃▅▆▇█`), throughput velocity, and wire-to-match latency jitter.
  * **Level 2 Market Depth Ladder**: Tight dual-sided book showing bids/asks with proportional visual depth bars and mid-market spread ribbons.
  * **Live Time & Sales Tape**: Executions ticker with tick indicators (▲/▼), notional turnover, and sub-microsecond latency SLA profiling.
  * **Institutional Whale Radar**: Real-time multi-level sweep and large block trade anomaly classification.
* **How to stop it**: Press `Ctrl + C`. Cleanly returns to your shell prompt.

---

## 3. 🌐 Interactive Web Trading Cockpit (`web_app.py`)

If you want to **select a preferred company's stock, place manual Buy/Sell orders, analyze charts, and run trading bots**, use `web_app.py`.

---

### 3.1 Default Launch (Auto-Opens Browser)
```powershell
python web_app.py
```
* **What it does**: Starts the FastAPI + WebSocket backend on `http://127.0.0.1:8000` and automatically opens your default web browser.
* **Default Symbol**: Opens `BTC-USD` or `SOL-USD`.

---

### 3.2 Launch Directly with a Preferred Company / Stock Symbol
```powershell
# Open with NVIDIA (NVDA)
python web_app.py --symbol NVDA-USD

# Open with Bitcoin (BTC)
python web_app.py --symbol BTC-USD

# Open with Apple (AAPL)
python web_app.py --symbol AAPL
```
* **What it does**: Launches the trading cockpit with your chosen company's order book, historical candlesticks, and live trade tape preloaded.

---

### 3.3 Custom Port or Headless Server Mode
```powershell
# Run on port 8080 without auto-opening the browser window
python web_app.py --port 8080 --no-browser
```
* **Result**: Navigate to `http://127.0.0.1:8080` manually in Chrome, Edge, or Brave.

---

### 3.4 Features to Show in the Web Cockpit:
1. **Anti-Compression Dynamic Focus Charting**: When big downward or upward impulse candles occur, preceding candles remain full-size, distinct, and readable without shrinking into flat slivers. Viewport bounds interpolate smoothly using EMA dampening.
2. **Session Persistence**: All generated and matched candles persist in `fluxcore_market_history.db`. When you close and restart the server, the chart and order book resume **exactly where you left off**.
3. **Multi-Timeframe Institutional CSV Exporter**: Click `Export CSV ▾` on the chart toolbar to export:
   - **Durations**: `Last 30 Min`, `Last 1 Hour`, `Last 2 Hours (Maximum)`, or `All Session Data`
   - **Scopes**: `All Different Stocks (Combined Tape)` or `Current Active Stock`
   - **Data Types**: `Orders & Executed Trades Tape` or `1-Minute OHLCV Candlesticks`
4. **Simple Mode vs. Pro Cockpit**: Toggle between clean retail view and institutional multi-panel quant view.
5. **Order Placement Form**: Buy/Sell with Limit/Market, Time-in-Force (`GTC`, `IOC`, `FOK`), and Post-Only protection.
6. **7-Tab Institutional Workstation**:
   - `Tab 1: Open Orders & Execution Stream`: View resting orders, 1-click cancel, or click the `PANIC CANCEL ALL` button.
   - `Tab 2: Live Tape & Whale Detector`: Microsecond trade feed with whale trade highlights.
   - `Tab 3: Algo Desk`: Toggle Avellaneda-Stoikov market maker bot or DQN RL agent.
   - `Tab 4: AI Pattern Radar`: Real-time Market Regime Classifier & Order Flow Imbalance (OFI).
   - `Tab 5: Quant Tear Sheet & Monte Carlo`: Interactive backtest runner and 1,000-path Monte Carlo risk cones.
   - `Tab 6: Multi-Venue SOR & Arbitrage`: View cross-exchange prices (NASDAQ, NYSE, BATS, IEX) and crossed-market arbitrage snipes.
   - `Tab 7: Risk & Margin`: Master circuit breaker halt button and margin status.

---

## 4. 🛒 Live Stock Order Placement Commands (REST / CLI / Python)

While `web_app.py` is running, you can place orders programmatically from PowerShell, terminal, or Python scripts.

---

### 4.1 Buy a Preferred Stock via PowerShell (e.g., NVDA)
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/order" -Method Post -ContentType "application/json" -Body '{
  "symbol": "NVDA-USD",
  "side": "BUY",
  "order_type": "LIMIT",
  "price": 130.00,
  "quantity": 100
}'
```
* **Expected Result**: Returns JSON confirmation:
  ```json
  {"status": "PLACED", "order_id": 1001, "symbol": "NVDA-USD", "side": "BUY", "quantity": 100, "price": 130.0}
  ```

---

### 4.2 Buy a Preferred Stock via `curl` (e.g., BTC or AAPL)
```bash
# Buy 1 Bitcoin at $65,000
curl -X POST http://127.0.0.1:8000/api/order \
  -H "Content-Type: application/json" \
  -d '{"symbol": "BTC-USD", "side": "BUY", "order_type": "LIMIT", "price": 65000.00, "quantity": 1}'

# Buy 200 shares of Apple at Market Price
curl -X POST http://127.0.0.1:8000/api/order \
  -H "Content-Type: application/json" \
  -d '{"symbol": "AAPL", "side": "BUY", "order_type": "MARKET", "quantity": 200}'
```

---

### 4.3 Buy a Preferred Stock via Python Script
```python
import requests

order_payload = {
    "symbol": "NVDA-USD",
    "side": "BUY",
    "order_type": "LIMIT",
    "price": 132.50,
    "quantity": 250,
    "tif": "GTC"
}

resp = requests.post("http://127.0.0.1:8000/api/order", json=order_payload)
print("Order Status:", resp.json())
```

---

### 4.4 Cancel an Existing Order
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/order/cancel" -Method Post -ContentType "application/json" -Body '{
  "order_id": 1001,
  "symbol": "NVDA-USD"
}'
```

---

### 4.5 Export Order Execution Tape & Market Data via API (30m, 1h, 2h, All Stocks)
```powershell
# Download Last 2 Hours of trade executions across ALL stocks
Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/market/export-csv?symbol=ALL&duration=2h&data_type=trades" -OutFile "FluxCore_ALL_STOCKS_2H_Trades.csv"

# Download Last 1 Hour of trade executions for Bitcoin (BTC-USD)
Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/market/export-csv?symbol=BTC-USD&duration=1h&data_type=trades" -OutFile "FluxCore_BTC_1H_Trades.csv"

# Download Last 30 Minutes of OHLCV Candlesticks for NVIDIA (NVDA-USD)
Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/market/export-csv?symbol=NVDA-USD&duration=30m&data_type=candles" -OutFile "FluxCore_NVDA_30M_Candles.csv"
```

---

### 4.5 Inspect Active Market Snapshot & L2 Depth
```powershell
# Get active symbol snapshot (NVDA-USD)
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/snapshot?symbol=NVDA-USD"
```
* **Result**: Returns top bids, asks, spread, last traded price, 24h volume, and current volatility.

---

### 4.6 Trigger Almgren-Chriss / DQN RL Child Order Slicer
```powershell
# Slice an institutional parent order of 5,000 shares of NVDA over 60 seconds
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/algo/rl/execute" -Method Post -ContentType "application/json" -Body '{
  "symbol": "NVDA-USD",
  "side": "BUY",
  "shares": 5000,
  "horizon_sec": 60.0,
  "risk_aversion": 0.001
}'
```
* **Result**: Starts the reinforcement learning agent that slices the 5,000 shares into micro-batches to avoid market impact.

---

## 5. 🎬 Executive Showcase & Market Stress Scenarios (`demo.py`)

`demo.py` is an automated, scripted executive demonstration designed for live presentations and project reviews.

### Run the 4-Phase Market Showcase
```powershell
python demo.py
```
* **What it does**: Runs 4 realistic market regimes sequentially with live terminal visual frames:
  1. **Phase 1: Standard Liquidity Provisioning**: Market makers seeding the initial bids and asks.
  2. **Phase 2: Institutional Whale Sweeps**: An institutional buyer executing an aggressive multi-level sweep (>5,000 shares), highlighting the Whale Alert banner.
  3. **Phase 3: Cancellation Storm**: High-frequency algorithmic cancellation storm demonstrating $O(1)$ order unlinking in the C hash table.
  4. **Phase 4: Earnings Shock & Flash Volatility Surge**: Rapid-fire execution cross-trades and spread widening.
* **Why run it during a review**: It showcases edge cases without requiring you to manually trigger them.

---

## 6. 📈 Data Ingestion & Machine Learning Scripts

FluxCore includes institutional quantitative data tooling and an ML training pipeline.

---

### 6.1 Download 1-Minute Sample Intraday Kline Data
```powershell
python scripts/download_sample_data.py
```
* **What it does**: Downloads free public 1-minute historical datasets from Binance Vision for `BTCUSDT`, `ETHUSDT`, and `SOLUSDT` into `data/intraday/` without requiring any API keys.
* **Expected Result**: Creates `.csv` files inside `data/intraday/`.

---

### 6.2 Train Institutional Microstructure Regime ML Classifier
```powershell
python scripts/train_ml_model.py
```
* **What it does**:
  * Ingests historical intraday bars and extracts 9 quantitative microstructure features (RSI, MACD, Bollinger Bandwidth, ATR ratio, Momentum 5d/10d, Volume ratio, Volatility, Price acceleration).
  * Performs an 80/20 chronological walk-forward train/test split.
  * Trains a Gaussian Naive Bayes classifier with empirical Bayes variance shrinkage.
  * Evaluates against institutional metrics: Confusion Matrix, Macro F1, Matthews Correlation Coefficient (MCC), and Directional Hit Rate (DHR).
  * Exports optimized weights to `data/ml_weights.json` and `fluxcore/market/ml_weights.json`.
* **Expected Result**: Prints confusion matrices and validation metrics directly to the terminal.

### 6.3 Quantitative Strategy Backtester & Bloomberg Tear Sheet CLI (`backtest.py`)
```powershell
# Run Dual EMA Trend Breakout Strategy on NVIDIA with 1,000-path Monte Carlo stress test
python backtest.py --symbol NVDA --strategy dual_ema --monte-carlo

# Run Bollinger Bands Mean Reversion Strategy on Apple with CSV trade log export
python backtest.py --symbol AAPL --strategy bollinger_reversion --export-csv trades_aapl.csv

# Run Bayesian Order Flow Momentum Strategy on Bitcoin
python backtest.py --symbol BTC-USD --strategy bayesian_flow --capital 250000
```
* **What it does**:
  * Simulates algorithmic quantitative alpha strategies across real OHLCV historical bars.
  * Models realistic institutional transaction costs: exchange fee commissions (bps) and execution market impact slippage (bps).
  * Computes Bloomberg PORT / PyFolio-grade risk statistics: Sharpe ratio, Sortino ratio, Calmar ratio, Jensen's Alpha, Beta, Maximum Drawdown (MDD), and Parametric & Historical Value at Risk (VaR 95%, VaR 99%, CVaR / Expected Shortfall).
  * Renders an authentic institutional terminal Unicode equity curve and risk scorecard.
  * Performs a forward 1,000-path Monte Carlo simulation forecasting tail ruin probability.
* **Available Strategies**: `dual_ema`, `bollinger_reversion`, `bayesian_flow`, `twap_benchmark`.

---

## 7. 🧪 Comprehensive Verification & Test Suite (`pytest`)

FluxCore comes with **173 automated tests** verifying every module.

---

### 7.1 Run Entire Test Suite (173 Tests)
```powershell
pytest -v
```
* **Expected Result**: `173 passed in ~4.5 seconds`.

---

### 7.2 Run Specific Domain Tests

| Target Domain | Command | What It Verifies |
| :--- | :--- | :--- |
| **Pure C Order Book** | `pytest tests/test_order_book.py -v` | FIFO price-time priority, matching logic, partial fills. |
| **Shared Memory IPC** | `pytest tests/test_ring_buffer.py -v` | SPSC lock-free ring buffer, cache-line alignment, multi-process memory mapping. |
| **Risk Engine** | `pytest tests/test_risk_engine.py -v` | Pre-trade risk checks, max order limits, credit checks, circuit breakers. |
| **Smart Order Router** | `pytest tests/test_smart_order_router.py -v` | 4 SOR routing policies across 4 exchanges, latency arbitrage snipes. |
| **Quant & Monte Carlo**| `pytest tests/test_backtest_monte_carlo.py -v`| Sharpe/Sortino ratios, 1,000-path Monte Carlo simulations, VaR 95/99%. |
| **RL Execution Agent** | `pytest tests/test_rl_execution.py -v` | Deep Q-Network order slicing, Almgren-Chriss gym environment. |
| **Web Server & APIs** | `pytest tests/test_web_server.py -v` | FastAPI endpoints, WebSocket feeds, order placement verification. |
| **WAL Recovery** | `pytest tests/test_wal_recovery.py -v` | SQLite crash recovery, state replay from disk. |

---

## 8. ⚡ Quick-Reference Cheat Sheet

| Task | Command |
| :--- | :--- |
| **Build C Engine** | `python setup.py build_ext --inplace` |
| **Run Exchange TUI (100k Orders)** | `python main.py --orders 100000 --rate 100000` |
| **Run Continuous Exchange TUI** | `python main.py --orders 0` |
| **Run Headless 500k Soak Benchmark** | `python main.py --no-ui --orders 500000` |
| **Start Web Trading Cockpit** | `python web_app.py` |
| **Start Web Cockpit for NVIDIA** | `python web_app.py --symbol NVDA-USD` |
| **Start Web Cockpit for Apple** | `python web_app.py --symbol AAPL` |
| **Run Automated Executive Showcase** | `python demo.py` |
| **Train ML Regime Model** | `python scripts/train_ml_model.py` |
| **Download Market Data** | `python scripts/download_sample_data.py` |
| **Run All 173 Tests** | `pytest -v` |
