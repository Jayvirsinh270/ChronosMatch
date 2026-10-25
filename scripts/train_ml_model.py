"""
ChronosMatch: Offline Historical ML Trainer.

Scans all available stock/ETF OHLCV CSVs in the data/ directory,
computes rich technical indicator features for each bar, assigns
ground-truth regime labels from forward returns, and trains a
Gaussian Naive Bayes classifier entirely in pure Python (no sklearn).

Output: data/ml_weights.json
  - Per-regime Gaussian means/variances for each feature
  - Regime base-rate priors
  - Calibrated Bayesian feature weights (w_ofi, w_price_trend, w_autocorr)
  - Per-stock volatility/volume profiles for seeding online learners

Usage:
    python scripts/train_ml_model.py
"""

import json
import math
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
OUTPUT_PATH = os.path.join(DATA_ROOT, "ml_weights.json")
PKG_OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "chronos_match", "market", "ml_weights.json")

# Regime label thresholds
BULL_THRESHOLD = 0.015      # 5-day forward return > +1.5% → BULL
BEAR_THRESHOLD = -0.015     # 5-day forward return < -1.5% → BEAR
CHOP_VOL_RATIO = 1.6        # ATR ratio > 1.6x rolling mean → CHOP
FORWARD_BARS = 5            # Prediction horizon for labeling

REGIME_BULL = "MOMENTUM_BREAKOUT_BULL"
REGIME_BEAR = "MOMENTUM_BREAKOUT_BEAR"
REGIME_CHOP = "VOLATILITY_EXPANSION_CHOP"
REGIME_CONSOL = "BALANCED_CONSOLIDATION"
ALL_REGIMES = [REGIME_BULL, REGIME_BEAR, REGIME_CHOP, REGIME_CONSOL]

# Feature names (order matters)
FEATURE_NAMES = [
    "rsi14",
    "macd_signal",
    "bb_bandwidth",
    "atr_ratio",
    "momentum_5d",
    "momentum_10d",
    "volume_ratio",
    "vol_10d",
    "price_acceleration",
]

# ---------------------------------------------------------------------------
# CSV Reading
# ---------------------------------------------------------------------------

def _parse_csv(filepath: str) -> List[Dict]:
    """Parse a US-style or BSE-style OHLCV CSV. Returns list of bar dicts."""
    bars = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            header = [h.strip() for h in f.readline().split(",")]
            is_bse = "Open Price" in header or "No.of Shares" in header

            for line in f:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 5 or not parts[0]:
                    continue
                try:
                    if is_bse:
                        o, h, l, c = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
                        v = int(float(parts[6])) if len(parts) > 6 else 1000
                    else:
                        o, h, l, c = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
                        v = int(float(parts[6])) if len(parts) > 6 else 1000
                    if c <= 0 or h <= 0:
                        continue
                    bars.append({"o": o, "h": h, "l": l, "c": c, "v": v})
                except (ValueError, IndexError):
                    continue
    except Exception:
        pass

    if is_bse and bars:
        bars.reverse()
    return bars


# ---------------------------------------------------------------------------
# Technical Indicator Computation
# ---------------------------------------------------------------------------

def _compute_rsi(closes: List[float], period: int = 14) -> List[float]:
    """Wilder's RSI for each bar (returns NaN for first `period` bars)."""
    rsi_vals = [float("nan")] * len(closes)
    if len(closes) <= period:
        return rsi_vals

    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    alpha = 1.0 / period
    for i in range(period, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = (1 - alpha) * avg_gain + alpha * gain
        avg_loss = (1 - alpha) * avg_loss + alpha * loss
        if avg_loss < 1e-10:
            rsi_vals[i] = 99.0
        else:
            rs = avg_gain / avg_loss
            rsi_vals[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsi_vals


def _compute_macd_signal(closes: List[float]) -> List[float]:
    """MACD line (12 EMA - 26 EMA) minus 9-period Signal EMA, normalised by price."""
    n = len(closes)
    signal_vals = [0.0] * n
    if n < 35:
        return signal_vals

    def _ema(src: List[float], period: int) -> List[float]:
        k = 2.0 / (period + 1)
        ema = [0.0] * len(src)
        ema[0] = src[0]
        for i in range(1, len(src)):
            ema[i] = src[i] * k + ema[i - 1] * (1 - k)
        return ema

    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_line = [ema12[i] - ema26[i] for i in range(n)]
    signal_line = _ema(macd_line[25:], 9)  # Only compute after 26 bars

    for i in range(len(signal_line)):
        idx = i + 25 + 9
        if idx < n:
            p = max(0.01, closes[idx])
            signal_vals[idx] = (macd_line[idx] - signal_line[i]) / p

    return signal_vals


def _compute_bb_bandwidth(closes: List[float], period: int = 20) -> List[float]:
    """Bollinger Band width as fraction of middle band."""
    bw = [0.0] * len(closes)
    for i in range(period, len(closes)):
        window = closes[i - period:i]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        std = math.sqrt(max(1e-10, var))
        bw[i] = (4.0 * std) / max(0.01, mean)
    return bw


def _compute_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    """Average True Range (Wilder smoothed)."""
    n = len(closes)
    atrs = [0.0] * n
    if n < period + 1:
        return atrs

    trs = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)

    atr = sum(trs[:period]) / period
    atrs[period] = atr
    alpha = 1.0 / period
    for i in range(period, n - 1):
        tr = trs[i]
        atr = (1 - alpha) * atr + alpha * tr
        atrs[i + 1] = atr
    return atrs


def _compute_volume_ratio(volumes: List[int], period: int = 20) -> List[float]:
    """Today's volume / rolling mean volume."""
    vr = [1.0] * len(volumes)
    for i in range(period, len(volumes)):
        mean_v = sum(volumes[i - period:i]) / period
        vr[i] = volumes[i] / max(1, mean_v)
    return vr


def _compute_features(bars: List[Dict]) -> Optional[List[Tuple[List[float], str]]]:
    """
    Compute features + regime labels for every bar in a stock's history.
    Returns list of (feature_vector, regime_label) for bars with full data.
    """
    if len(bars) < 40:
        return None

    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    volumes = [b["v"] for b in bars]
    n = len(bars)

    rsi_vals = _compute_rsi(closes, 14)
    macd_vals = _compute_macd_signal(closes)
    bb_vals = _compute_bb_bandwidth(closes, 20)
    atr_vals = _compute_atr(highs, lows, closes, 14)
    vr_vals = _compute_volume_ratio(volumes, 20)

    # Rolling ATR mean for ratio
    atr_means = [0.0] * n
    for i in range(40, n):
        window = atr_vals[i - 20:i]
        if window:
            atr_means[i] = sum(window) / len(window)

    # Rolling 10-day log-return std
    log_returns = [0.0] + [math.log(closes[i] / max(0.01, closes[i - 1])) for i in range(1, n)]
    vol10 = [0.0] * n
    for i in range(10, n):
        window = log_returns[i - 10:i]
        mean_r = sum(window) / 10
        var = sum((r - mean_r) ** 2 for r in window) / 10
        vol10[i] = math.sqrt(max(1e-12, var)) * math.sqrt(252)

    samples = []
    for i in range(40, n - FORWARD_BARS):
        if math.isnan(rsi_vals[i]):
            continue

        p = closes[i]
        if p <= 0:
            continue

        # ----- Features -----
        rsi = rsi_vals[i]
        macd_s = macd_vals[i]
        bb = bb_vals[i]
        atr_r = atr_vals[i] / max(0.001, atr_means[i]) if atr_means[i] > 0 else 1.0
        mom5 = (closes[i] - closes[i - 5]) / max(0.01, closes[i - 5])
        mom10 = (closes[i] - closes[i - 10]) / max(0.01, closes[i - 10])
        vr = vr_vals[i]
        vol = vol10[i]
        p_accel = (closes[i] - 2 * closes[i - 1] + closes[i - 2]) / max(0.01, p) if i >= 2 else 0.0

        feat = [rsi, macd_s * 1000, bb * 100, atr_r, mom5 * 100, mom10 * 100, vr, vol * 100, p_accel * 1000]

        # ----- Forward return label -----
        fwd_ret = (closes[i + FORWARD_BARS] - p) / p
        atr_now = atr_vals[i]
        atr_mean = atr_means[i] if atr_means[i] > 0 else atr_now

        if fwd_ret > BULL_THRESHOLD:
            label = REGIME_BULL
        elif fwd_ret < BEAR_THRESHOLD:
            label = REGIME_BEAR
        elif atr_now > atr_mean * CHOP_VOL_RATIO:
            label = REGIME_CHOP
        else:
            label = REGIME_CONSOL

        samples.append((feat, label))

    return samples if samples else None


# ---------------------------------------------------------------------------
# Gaussian Naive Bayes
# ---------------------------------------------------------------------------

class GaussianNaiveBayes:
    """Pure-Python Gaussian Naive Bayes classifier."""

    def __init__(self, classes: List[str], n_features: int):
        self.classes = classes
        self.n_features = n_features
        self.counts: Dict[str, int] = {c: 0 for c in classes}
        self.sums: Dict[str, List[float]] = {c: [0.0] * n_features for c in classes}
        self.sq_sums: Dict[str, List[float]] = {c: [0.0] * n_features for c in classes}
        self.total = 0

    def partial_fit(self, features: List[float], label: str):
        if label not in self.counts:
            return
        self.counts[label] += 1
        self.total += 1
        for j, x in enumerate(features):
            self.sums[label][j] += x
            self.sq_sums[label][j] += x * x

    def finalize(self) -> Dict:
        """Compute means and variances, return serializable weight dict."""
        params = {}
        for c in self.classes:
            n = max(1, self.counts[c])
            means = [self.sums[c][j] / n for j in range(self.n_features)]
            vars_ = [
                max(1e-6, self.sq_sums[c][j] / n - means[j] ** 2)
                for j in range(self.n_features)
            ]
            params[c] = {"mean": means, "var": vars_, "count": n}
        return params

    def log_prob(self, features: List[float], params: Dict) -> Dict[str, float]:
        """Compute log posterior (unnormalized) for each class."""
        log_probs = {}
        for c, p in params.items():
            prior = math.log(max(1e-10, self.counts[c] / max(1, self.total)))
            ll = prior
            for j, x in enumerate(features):
                mean = p["mean"][j]
                var = p["var"][j]
                ll -= 0.5 * math.log(2 * math.pi * var)
                ll -= (x - mean) ** 2 / (2 * var)
            log_probs[c] = ll
        return log_probs


# ---------------------------------------------------------------------------
# Stock profile extraction
# ---------------------------------------------------------------------------

def _compute_stock_profile(bars: List[Dict]) -> Dict:
    """Return volatility and avg volume summary for a stock."""
    if not bars:
        return {"daily_vol": 0.02, "avg_volume": 100_000, "latest_close": 100.0}
    closes = [b["c"] for b in bars[-252:]]
    vols = [b["v"] for b in bars[-252:]]
    log_rets = [math.log(closes[i] / max(0.01, closes[i - 1])) for i in range(1, len(closes))]
    if log_rets:
        mean_r = sum(log_rets) / len(log_rets)
        var = sum((r - mean_r) ** 2 for r in log_rets) / len(log_rets)
        daily_vol = math.sqrt(max(1e-10, var))
    else:
        daily_vol = 0.02
    avg_vol = int(sum(vols) / max(1, len(vols)))
    return {
        "daily_vol": round(daily_vol, 6),
        "avg_volume": avg_vol,
        "latest_close": round(closes[-1], 4) if closes else 100.0,
    }


# ---------------------------------------------------------------------------
# Main Training Loop
# ---------------------------------------------------------------------------

def collect_csv_files(data_root: str) -> List[str]:
    """Walk data/ for all CSV files."""
    files = []
    for root, dirs, filenames in os.walk(data_root):
        # Skip meta/index files
        dirs[:] = [d for d in dirs if d not in ["__pycache__"]]
        for fname in filenames:
            if fname.endswith(".csv") and "symbols_valid_meta" not in fname and "CompanyName" not in fname:
                files.append(os.path.join(root, fname))
    return files


def train(max_files: int = 800, verbose: bool = True) -> Dict:
    """
    Full offline training pipeline.
    Returns the weight dict that will be saved as ml_weights.json.
    """
    t_start = time.time()
    csv_files = collect_csv_files(DATA_ROOT)
    if verbose:
        print(f"[ChronosML] Found {len(csv_files)} CSV files in data/")

    gnb = GaussianNaiveBayes(ALL_REGIMES, len(FEATURE_NAMES))

    # Priority tickers to always include
    priority = {"NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META", "JPM", "BRK-B", "SPY", "QQQ"}
    priority_files = [f for f in csv_files if os.path.splitext(os.path.basename(f))[0].upper() in priority]
    other_files = [f for f in csv_files if f not in priority_files]

    # Limit to max_files for speed; always include priority
    selected = priority_files + other_files[:max(0, max_files - len(priority_files))]

    total_samples = 0
    regime_counts: Dict[str, int] = {r: 0 for r in ALL_REGIMES}
    stock_profiles: Dict[str, Dict] = {}

    skipped = 0
    for i, fp in enumerate(selected):
        ticker = os.path.splitext(os.path.basename(fp))[0].upper()
        bars = _parse_csv(fp)
        if len(bars) < 50:
            skipped += 1
            continue

        samples = _compute_features(bars)
        if not samples:
            skipped += 1
            continue

        for feat, label in samples:
            gnb.partial_fit(feat, label)
            regime_counts[label] += 1
            total_samples += 1

        # Store profile for popular tickers
        if ticker in priority or len(stock_profiles) < 200:
            stock_profiles[ticker] = _compute_stock_profile(bars)

        if verbose and (i + 1) % 100 == 0:
            elapsed = time.time() - t_start
            print(f"  [{i + 1}/{len(selected)}] {total_samples:,} samples | {elapsed:.1f}s")

    if total_samples < 100:
        print("[ChronosML] ERROR: Not enough training data found. Check data/ directory.")
        sys.exit(1)

    if verbose:
        print(f"\n[ChronosML] Training complete: {total_samples:,} samples from {len(selected) - skipped} stocks")
        for r, cnt in regime_counts.items():
            pct = 100.0 * cnt / max(1, total_samples)
            print(f"  {r}: {cnt:,} ({pct:.1f}%)")

    # ---- Finalize GNB parameters ----
    gnb_params = gnb.finalize()

    # ---- Compute regime priors ----
    priors = {r: regime_counts[r] / max(1, total_samples) for r in ALL_REGIMES}

    # ---- Derive calibrated feature weights ----
    # Extract discriminative power of OFI-proxy (momentum_5d), price trend, and autocorr
    # by computing signal-to-noise ratio between BULL and BEAR means for each feature
    bull_means = gnb_params[REGIME_BULL]["mean"]
    bear_means = gnb_params[REGIME_BEAR]["mean"]
    consol_means = gnb_params[REGIME_CONSOL]["mean"]
    chop_means = gnb_params[REGIME_CHOP]["mean"]

    # Feature indices: 0=RSI, 1=MACD_sig, 2=BB_bw, 3=ATR_ratio, 4=mom5, 5=mom10, 6=vol_ratio, 7=vol, 8=accel
    def _snr(bull_m, bear_m, pool_var) -> float:
        diff = abs(bull_m - bear_m)
        return diff / max(1e-6, math.sqrt(pool_var))

    def _pool_var(c1_mean, c1_var, c1_n, c2_mean, c2_var, c2_n) -> float:
        # Pooled variance
        total_n = c1_n + c2_n
        if total_n < 2:
            return 1.0
        return ((c1_n - 1) * c1_var + (c2_n - 1) * c2_var) / (total_n - 2)

    feat_snrs = []
    for j in range(len(FEATURE_NAMES)):
        pv = _pool_var(
            bull_means[j], gnb_params[REGIME_BULL]["var"][j], gnb_params[REGIME_BULL]["count"],
            bear_means[j], gnb_params[REGIME_BEAR]["var"][j], gnb_params[REGIME_BEAR]["count"],
        )
        feat_snrs.append(_snr(bull_means[j], bear_means[j], pv))

    # Map SNR of features to Bayesian weights in the online model
    # mom5 (idx 4) → most similar to OFI signal
    # MACD signal (idx 1) → price trend proxy
    # vol10 (idx 7) → autocorrelation/volatility proxy
    snr_sum = sum(feat_snrs) + 1e-9
    w_ofi = round(1.2 + 1.3 * (feat_snrs[4] / snr_sum) * len(feat_snrs), 4)   # momentum_5d
    w_trend = round(1.0 + 0.8 * (feat_snrs[1] / snr_sum) * len(feat_snrs), 4)  # macd_signal
    w_autocorr = round(0.6 + 0.5 * (feat_snrs[7] / snr_sum) * len(feat_snrs), 4)  # vol_10d

    # Clamp to safe ranges
    w_ofi = max(1.2, min(3.0, w_ofi))
    w_trend = max(0.8, min(2.5, w_trend))
    w_autocorr = max(0.4, min(1.8, w_autocorr))

    # ---- Assemble output ----
    weights = {
        "version": "2.0",
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_samples": total_samples,
        "total_stocks": len(selected) - skipped,
        "feature_names": FEATURE_NAMES,
        "gnb_params": gnb_params,
        "priors": priors,
        "regime_counts": regime_counts,
        "w_ofi": w_ofi,
        "w_price_trend": w_trend,
        "w_autocorr": w_autocorr,
        "stock_profiles": stock_profiles,
        "feature_snr": {FEATURE_NAMES[j]: round(feat_snrs[j], 4) for j in range(len(FEATURE_NAMES))},
    }

    elapsed = time.time() - t_start
    if verbose:
        print(f"\n[ChronosML] Derived weights:")
        print(f"  w_ofi={w_ofi}, w_price_trend={w_trend}, w_autocorr={w_autocorr}")
        print(f"  Feature SNR: {weights['feature_snr']}")
        print(f"\n[ChronosML] Total time: {elapsed:.1f}s")

    return weights


def main():
    print("=" * 60)
    print("  ChronosMatch Offline ML Trainer  v2.0")
    print("=" * 60)
    weights = train(max_files=800, verbose=True)
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(weights, f, indent=2)
    os.makedirs(os.path.dirname(PKG_OUTPUT_PATH), exist_ok=True)
    with open(PKG_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(weights, f, indent=2)
    print(f"\n[ChronosML] Weights saved to {OUTPUT_PATH} and {PKG_OUTPUT_PATH}")
    print("[ChronosML] Run 'python -m chronos_match' to start the server with pre-trained model.")


if __name__ == "__main__":
    main()
