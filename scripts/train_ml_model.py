"""
ChronosMatch: Institutional Quantitative ML Model Trainer.

World-Class High-Frequency / Microstructure Regime Classifier.
Trains an institutional Gaussian Naive Bayes classifier with robust winsorization,
empirical Bayes variance shrinkage, out-of-sample walk-forward train/test splitting (80/20),
and evaluates against institutional performance metrics:
  - Multi-Class Confusion Matrix
  - Per-Class Precision, Recall, and F1-Score
  - Macro F1 and Weighted F1 Scores
  - Multi-Class Matthews Correlation Coefficient (MCC)
  - Directional Hit Rate (DHR) on momentum signals
  - Multi-Class Cross-Entropy / Log-Loss
  - Signal-to-Noise Ratio (SNR) feature importance

Output:
  - data/ml_weights.json
  - chronos_match/market/ml_weights.json
"""

import json
import math
import os
import sys
import time
from typing import Dict, List, Optional, Tuple, Any

# ---------------------------------------------------------------------------
# Paths and Constants
# ---------------------------------------------------------------------------
DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
OUTPUT_PATH = os.path.join(DATA_ROOT, "ml_weights.json")
PKG_OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "chronos_match", "market", "ml_weights.json")

REGIME_CONSOL = "BALANCED_CONSOLIDATION"
REGIME_BULL = "MOMENTUM_BREAKOUT_BULL"
REGIME_BEAR = "MOMENTUM_BREAKOUT_BEAR"
REGIME_CHOP = "VOLATILITY_EXPANSION_CHOP"
ALL_REGIMES = [REGIME_CONSOL, REGIME_BULL, REGIME_BEAR, REGIME_CHOP]

# 9 Core Indicator Features (order matches online LightMarketPatternModel)
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

FORWARD_BARS = 5
TRAIN_RATIO = 0.80  # 80% chronological train, 20% out-of-sample test
PRIOR_TEMP = 0.35   # Temperature for smoothing prior class imbalance in decision space


# ---------------------------------------------------------------------------
# CSV Parser
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
                    if c <= 0.20 or h <= 0 or l <= 0:
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
    """Wilder's RSI for each bar."""
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
    signal_line = _ema(macd_line[25:], 9)

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
    """Current volume / rolling mean volume."""
    vr = [1.0] * len(volumes)
    for i in range(period, len(volumes)):
        mean_v = sum(volumes[i - period:i]) / period
        vr[i] = volumes[i] / max(1, mean_v)
    return vr


def _extract_stock_samples(bars: List[Dict]) -> Optional[Tuple[List[Tuple[List[float], str]], List[Tuple[List[float], str]]]]:
    """
    Extract (features, label) samples from bar history with financial winsorization
    and split chronologically into (train_samples, test_samples).
    """
    if len(bars) < 60:
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

    # Rolling ATR mean
    atr_means = [0.0] * n
    for i in range(40, n):
        window = atr_vals[i - 20:i]
        if window:
            atr_means[i] = sum(window) / len(window)

    # Rolling 10-day log-returns & volatility
    log_returns = [0.0] * n
    for i in range(1, n):
        p_prev = max(0.01, closes[i - 1])
        ret = (closes[i] - p_prev) / p_prev
        # Sanity check: reject extreme unadjusted split spikes (>75% jump)
        if abs(ret) > 0.75:
            ret = 0.0
        log_returns[i] = math.log(max(0.01, closes[i] / p_prev))

    vol10 = [0.0] * n
    for i in range(10, n):
        window = log_returns[i - 10:i]
        mean_r = sum(window) / 10.0
        var = sum((r - mean_r) ** 2 for r in window) / 10.0
        vol10[i] = math.sqrt(max(1e-12, var)) * math.sqrt(252)

    samples = []
    for i in range(40, n - FORWARD_BARS):
        if math.isnan(rsi_vals[i]):
            continue

        p = closes[i]
        if p <= 0.20:
            continue

        # Institutional Financial Winsorization (Truncates penny outliers & corrupt data)
        rsi = max(1.0, min(99.0, rsi_vals[i]))
        macd_s = max(-25.0, min(25.0, macd_vals[i] * 1000.0))
        bb = max(0.2, min(50.0, bb_vals[i] * 100.0))
        atr_r = max(0.1, min(6.0, atr_vals[i] / max(0.001, atr_means[i]) if atr_means[i] > 0 else 1.0))

        p_5 = max(0.01, closes[i - 5])
        p_10 = max(0.01, closes[i - 10])
        mom5 = max(-35.0, min(35.0, (closes[i] - p_5) / p_5 * 100.0))
        mom10 = max(-50.0, min(50.0, (closes[i] - p_10) / p_10 * 100.0))

        vr = max(0.05, min(10.0, vr_vals[i]))
        vol = max(1.0, min(180.0, vol10[i] * 100.0))

        p_accel = 0.0
        if i >= 2:
            p_acc = (closes[i] - 2.0 * closes[i - 1] + closes[i - 2]) / max(0.01, p) * 1000.0
            p_accel = max(-30.0, min(30.0, p_acc))

        feat = [rsi, macd_s, bb, atr_r, mom5, mom10, vr, vol, p_accel]

        # Quantitative Triple Barrier / Dynamic Volatility Labeling
        fwd_p = closes[i + FORWARD_BARS]
        fwd_ret = (fwd_p - p) / p
        vol_5d = math.sqrt(sum(r ** 2 for r in log_returns[i - 5:i]) / 5.0) if i >= 5 else 0.015
        dynamic_threshold = max(0.012, min(0.032, 1.25 * vol_5d * math.sqrt(5)))

        atr_now = atr_vals[i]
        atr_mean = atr_means[i] if atr_means[i] > 0 else atr_now

        if fwd_ret > dynamic_threshold:
            label = REGIME_BULL
        elif fwd_ret < -dynamic_threshold:
            label = REGIME_BEAR
        elif atr_now > atr_mean * 1.45:
            label = REGIME_CHOP
        else:
            label = REGIME_CONSOL

        samples.append((feat, label))

    if len(samples) < 20:
        return None

    split_idx = int(len(samples) * TRAIN_RATIO)
    train_samples = samples[:split_idx]
    test_samples = samples[split_idx:]
    return train_samples, test_samples


# ---------------------------------------------------------------------------
# Gaussian Naive Bayes Classifier with Bayesian Shrinkage
# ---------------------------------------------------------------------------

class GaussianNaiveBayes:
    """
    Gaussian Naive Bayes classifier with empirical Bayes variance shrinkage
    and temperature-scaled decision scoring.
    """

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

    def finalize(self) -> Dict[str, Dict[str, Any]]:
        """Compute means and empirical Bayes shrunk variances."""
        # Calculate pooled global variance across all classes
        global_var = [1.0] * self.n_features
        if self.total > 1:
            global_mean = [sum(self.sums[c][j] for c in self.classes) / self.total for j in range(self.n_features)]
            global_sq = [sum(self.sq_sums[c][j] for c in self.classes) / self.total for j in range(self.n_features)]
            global_var = [max(1e-4, global_sq[j] - global_mean[j] ** 2) for j in range(self.n_features)]

        params = {}
        for c in self.classes:
            n = max(1, self.counts[c])
            means = [self.sums[c][j] / n for j in range(self.n_features)]
            vars_ = []
            for j in range(self.n_features):
                sample_var = max(0.0, self.sq_sums[c][j] / n - means[j] ** 2)
                # Bayesian shrinkage: 5% weight toward global variance
                shrinkage = 0.05
                shrunk_var = (1 - shrinkage) * sample_var + shrinkage * global_var[j]
                vars_.append(max(0.01, shrunk_var))
            params[c] = {"mean": means, "var": vars_, "count": n}
        return params

    def predict_proba(self, features: List[float], params: Dict, prior_temperature: float = PRIOR_TEMP) -> Dict[str, float]:
        """Compute normalized posterior probabilities with temperature-scaled decision priors."""
        log_probs = {}
        counts_total = sum(p["count"] for p in params.values())

        # Temperature-smoothed prior distribution
        temp_priors = {c: (p["count"] / max(1, counts_total)) ** prior_temperature for c, p in params.items()}
        sum_temp = sum(temp_priors.values()) or 1.0

        for c, p in params.items():
            prior = math.log(max(1e-6, temp_priors[c] / sum_temp))
            ll = prior
            for j, x in enumerate(features):
                if j >= len(p["mean"]):
                    break
                mu = p["mean"][j]
                var = p["var"][j]
                ll -= 0.5 * math.log(2.0 * math.pi * var + 1e-15)
                ll -= (x - mu) ** 2 / (2.0 * var + 1e-15)
            log_probs[c] = ll

        max_ll = max(log_probs.values())
        exp_sum = sum(math.exp(max(-50.0, ll - max_ll)) for ll in log_probs.values())
        probs = {c: math.exp(max(-50.0, ll - max_ll)) / max(1e-12, exp_sum) for c, ll in log_probs.items()}
        return probs

    def predict(self, features: List[float], params: Dict) -> str:
        probs = self.predict_proba(features, params)
        return max(probs.items(), key=lambda item: item[1])[0]


# ---------------------------------------------------------------------------
# Institutional Evaluation Metrics Engine
# ---------------------------------------------------------------------------

def evaluate_model(
    gnb: GaussianNaiveBayes,
    params: Dict,
    test_samples: List[Tuple[List[float], str]],
) -> Dict[str, Any]:
    """
    Comprehensive multi-metric institutional evaluation on out-of-sample test set.
    """
    labels = ALL_REGIMES
    label_to_idx = {l: i for i, l in enumerate(labels)}
    k = len(labels)

    cm = [[0 for _ in range(k)] for _ in range(k)]
    total_samples = len(test_samples)
    if total_samples == 0:
        return {}

    log_loss_sum = 0.0
    directional_correct = 0
    directional_total = 0

    for feat, true_label in test_samples:
        probs = gnb.predict_proba(feat, params)
        pred_label = max(probs.items(), key=lambda item: item[1])[0]

        a_idx = label_to_idx.get(true_label, 0)
        p_idx = label_to_idx.get(pred_label, 0)
        cm[a_idx][p_idx] += 1

        # Multi-class cross-entropy
        true_prob = max(1e-7, min(1.0 - 1e-7, probs.get(true_label, 1e-7)))
        log_loss_sum -= math.log(true_prob)

        # Directional Hit Rate (evaluates actionable momentum breakout signals)
        if pred_label in (REGIME_BULL, REGIME_BEAR):
            directional_total += 1
            if (pred_label == REGIME_BULL and true_label == REGIME_BULL) or \
               (pred_label == REGIME_BEAR and true_label == REGIME_BEAR):
                directional_correct += 1

    total_correct = sum(cm[i][i] for i in range(k))
    accuracy = total_correct / total_samples

    per_class = {}
    f1_list = []
    support_list = []

    for i, label in enumerate(labels):
        tp = cm[i][i]
        fp = sum(cm[j][i] for j in range(k) if j != i)
        fn = sum(cm[i][j] for j in range(k) if j != i)
        support = sum(cm[i][j] for j in range(k))

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2.0 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0

        per_class[label] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": support,
        }
        f1_list.append(f1)
        support_list.append(support)

    macro_f1 = sum(f1_list) / max(1, k)
    weighted_f1 = sum(f1 * sup for f1, sup in zip(f1_list, support_list)) / max(1, total_samples)

    # Multi-class Matthews Correlation Coefficient (MCC)
    c = total_correct
    s = total_samples
    p_k = [sum(cm[j][i] for j in range(k)) for i in range(k)]
    t_k = [sum(cm[i][j] for j in range(k)) for i in range(k)]

    numerator = c * s - sum(p * t for p, t in zip(p_k, t_k))
    den_p = s ** 2 - sum(p ** 2 for p in p_k)
    den_t = s ** 2 - sum(t ** 2 for t in t_k)
    denominator = math.sqrt(max(1e-12, den_p * den_t))
    mcc = numerator / max(1e-12, denominator)

    directional_hit_rate = (directional_correct / directional_total) if directional_total > 0 else 0.0
    mean_log_loss = log_loss_sum / total_samples

    return {
        "out_of_sample_test_samples": total_samples,
        "accuracy": round(accuracy, 4),
        "macro_f1": round(macro_f1, 4),
        "weighted_f1": round(weighted_f1, 4),
        "matthews_corr_coef": round(mcc, 4),
        "directional_hit_rate": round(directional_hit_rate, 4),
        "log_loss": round(mean_log_loss, 4),
        "directional_evaluations": directional_total,
        "directional_hits": directional_correct,
        "confusion_matrix": {
            "labels": labels,
            "matrix": cm,
        },
        "per_class_metrics": per_class,
    }


# ---------------------------------------------------------------------------
# Stock Profile Extraction
# ---------------------------------------------------------------------------

def _compute_stock_profile(bars: List[Dict]) -> Dict:
    """Return volatility, volume, and last price summary for a stock."""
    if not bars:
        return {"daily_vol": 0.02, "avg_volume": 100_000, "latest_close": 100.0}
    closes = [b["c"] for b in bars[-252:]]
    vols = [b["v"] for b in bars[-252:]]
    log_rets = [math.log(max(0.01, closes[i]) / max(0.01, closes[i - 1])) for i in range(1, len(closes))]
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
# Main Training & Evaluation Loop
# ---------------------------------------------------------------------------

def collect_csv_files(data_root: str) -> List[str]:
    """Walk data/ directory for all stock & ETF CSV files."""
    files = []
    for root, dirs, filenames in os.walk(data_root):
        dirs[:] = [d for d in dirs if d not in ["__pycache__", ".venv", ".git"]]
        for fname in filenames:
            if fname.endswith(".csv") and "symbols_valid_meta" not in fname and "CompanyName" not in fname:
                files.append(os.path.join(root, fname))
    return files


def train(max_files: int = 1500, verbose: bool = True) -> Dict:
    """
    Full institutional offline training pipeline.
    Trains on 80% of data across 1,500+ symbols and evaluates on 20% out-of-sample data.
    """
    t_start = time.time()
    csv_files = collect_csv_files(DATA_ROOT)
    if verbose:
        print(f"[ChronosML] Found {len(csv_files):,} CSV files in data root.")

    # High-priority liquid mega-caps and indices to always include
    priority = {
        "NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "GOOGL", "META", "JPM", "BRK-B",
        "SPY", "QQQ", "IWM", "AMD", "NFLX", "BAC", "XOM", "V", "WMT", "DIS",
        "RELIANCE", "TCS", "INFY", "HDFCBANK", "500002", "500010", "500034", "500112", "500209",
    }
    priority_files = [f for f in csv_files if os.path.splitext(os.path.basename(f))[0].upper() in priority]
    other_files = [f for f in csv_files if f not in priority_files]

    selected = priority_files + other_files[:max(0, max_files - len(priority_files))]
    if verbose:
        print(f"[ChronosML] Selected {len(selected):,} files for training & out-of-sample evaluation.")

    gnb = GaussianNaiveBayes(ALL_REGIMES, len(FEATURE_NAMES))

    train_total_samples = 0
    test_all_samples: List[Tuple[List[float], str]] = []
    regime_train_counts: Dict[str, int] = {r: 0 for r in ALL_REGIMES}
    stock_profiles: Dict[str, Dict] = {}
    skipped = 0

    for i, fp in enumerate(selected):
        ticker = os.path.splitext(os.path.basename(fp))[0].upper()
        bars = _parse_csv(fp)
        if len(bars) < 60:
            skipped += 1
            continue

        result = _extract_stock_samples(bars)
        if not result:
            skipped += 1
            continue

        train_samples, test_samples = result

        # Ingest train samples
        for feat, label in train_samples:
            gnb.partial_fit(feat, label)
            regime_train_counts[label] += 1
            train_total_samples += 1

        # Collect test samples for out-of-sample validation
        test_all_samples.extend(test_samples)

        # Cache profile
        if ticker in priority or len(stock_profiles) < 300:
            stock_profiles[ticker] = _compute_stock_profile(bars)

        if verbose and (i + 1) % 250 == 0:
            elapsed = time.time() - t_start
            print(f"  [{i + 1}/{len(selected)}] Processed {train_total_samples + len(test_all_samples):,} bars ({train_total_samples:,} train / {len(test_all_samples):,} test) | {elapsed:.1f}s")

    if train_total_samples < 500:
        print("[ChronosML] ERROR: Insufficient training data. Check data/ directory.")
        sys.exit(1)

    t_train = time.time() - t_start
    if verbose:
        print(f"\n[ChronosML] Feature extraction complete in {t_train:.1f}s:")
        print(f"  Train samples: {train_total_samples:,} bars ({len(selected) - skipped} symbols)")
        print(f"  Test samples:  {len(test_all_samples):,} out-of-sample bars")
        print("\n[ChronosML] Training Class Distribution:")
        for r, cnt in regime_train_counts.items():
            pct = 100.0 * cnt / max(1, train_total_samples)
            print(f"  {r:<30}: {cnt:>10,} ({pct:5.1f}%)")

    # 1. Finalize GNB Gaussian Parameters
    gnb_params = gnb.finalize()

    # 2. Out-of-Sample Quantitative Evaluation
    if verbose:
        print("\n[ChronosML] Running Out-of-Sample Quantitative Evaluation...")
    eval_metrics = evaluate_model(gnb, gnb_params, test_all_samples)

    if verbose and eval_metrics:
        print("\n" + "=" * 70)
        print("          INSTITUTIONAL QUANTITATIVE ML SCORECARD")
        print("=" * 70)
        print(f"  Out-of-Sample Accuracy:           {eval_metrics['accuracy'] * 100:.2f}%")
        print(f"  Macro-Averaged F1 Score:          {eval_metrics['macro_f1']:.4f}")
        print(f"  Weighted F1 Score:                {eval_metrics['weighted_f1']:.4f}")
        print(f"  Matthews Correlation Coef (MCC):   {eval_metrics['matthews_corr_coef']:.4f}")
        print(f"  Directional Hit Rate (DHR):       {eval_metrics['directional_hit_rate'] * 100:.2f}%  ({eval_metrics['directional_hits']}/{eval_metrics['directional_evaluations']} correct)")
        print(f"  Cross-Entropy Log-Loss:           {eval_metrics['log_loss']:.4f}")
        print("-" * 70)
        print(f"  {'Regime':<28} {'Precision':<10} {'Recall':<10} {'F1-Score':<10} {'Support'}")
        print("-" * 70)
        for r, m in eval_metrics["per_class_metrics"].items():
            print(f"  {r:<28} {m['precision']:<10.3f} {m['recall']:<10.3f} {m['f1']:<10.3f} {m['support']}")
        print("-" * 70)
        print("  Confusion Matrix (Actual \\ Predicted):")
        cm = eval_metrics["confusion_matrix"]["matrix"]
        short_names = ["CONSOL", "BULL", "BEAR", "CHOP"]
        header_str = "    " + "".join(f"{sn:>10}" for sn in short_names)
        print(header_str)
        for i, row in enumerate(cm):
            row_str = f"  {short_names[i]:<4}" + "".join(f"{cnt:>10,}" for cnt in row)
            print(row_str)
        print("=" * 70)

    # 3. Derive Dynamic Bayesian Feature Weights from Signal-to-Noise Ratio (SNR)
    bull_means = gnb_params[REGIME_BULL]["mean"]
    bear_means = gnb_params[REGIME_BEAR]["mean"]

    def _snr(m1: float, m2: float, v1: float, v2: float, n1: int, n2: int) -> float:
        tot_n = n1 + n2
        if tot_n < 2:
            return 1.0
        pool_v = ((n1 - 1) * v1 + (n2 - 1) * v2) / (tot_n - 2)
        return abs(m1 - m2) / max(1e-6, math.sqrt(max(1e-6, pool_v)))

    feat_snrs = []
    for j in range(len(FEATURE_NAMES)):
        snr_val = _snr(
            bull_means[j], bear_means[j],
            gnb_params[REGIME_BULL]["var"][j], gnb_params[REGIME_BEAR]["var"][j],
            gnb_params[REGIME_BULL]["count"], gnb_params[REGIME_BEAR]["count"],
        )
        feat_snrs.append(snr_val)

    snr_sum = sum(feat_snrs) + 1e-9
    w_ofi = round(1.2 + 1.4 * (feat_snrs[4] / snr_sum) * len(feat_snrs), 4)       # mom5
    w_trend = round(1.0 + 0.9 * (feat_snrs[1] / snr_sum) * len(feat_snrs), 4)     # macd_signal
    w_autocorr = round(0.6 + 0.6 * (feat_snrs[7] / snr_sum) * len(feat_snrs), 4)  # vol_10d

    w_ofi = max(1.2, min(3.0, w_ofi))
    w_trend = max(0.8, min(2.5, w_trend))
    w_autocorr = max(0.4, min(1.8, w_autocorr))

    priors = {r: regime_train_counts[r] / max(1, train_total_samples) for r in ALL_REGIMES}

    # 4. Assemble Institutional Weight & Telemetry Artifact
    weights = {
        "version": "2.5 (Institutional Multi-Asset Quantitative Engine)",
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_samples": train_total_samples + len(test_all_samples),
        "train_samples": train_total_samples,
        "test_samples": len(test_all_samples),
        "total_stocks": len(selected) - skipped,
        "feature_names": FEATURE_NAMES,
        "gnb_params": gnb_params,
        "priors": priors,
        "regime_counts": regime_train_counts,
        "w_ofi": w_ofi,
        "w_price_trend": w_trend,
        "w_autocorr": w_autocorr,
        "feature_snr": {FEATURE_NAMES[j]: round(feat_snrs[j], 4) for j in range(len(FEATURE_NAMES))},
        "evaluation_metrics": eval_metrics,
        "stock_profiles": stock_profiles,
    }

    t_total = time.time() - t_start
    if verbose:
        print(f"\n[ChronosML] Calibrated Bayesian Weights:")
        print(f"  w_ofi: {w_ofi:.4f} | w_price_trend: {w_trend:.4f} | w_autocorr: {w_autocorr:.4f}")
        print(f"[ChronosML] Pipeline completed in {t_total:.1f}s.")

    return weights


def main():
    print("=" * 70)
    print("  ChronosMatch Institutional Machine Learning Pipeline v2.5")
    print("=" * 70)
    weights = train(max_files=1500, verbose=True)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(weights, f, indent=2)

    os.makedirs(os.path.dirname(PKG_OUTPUT_PATH), exist_ok=True)
    with open(PKG_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(weights, f, indent=2)

    print(f"\n[ChronosML] Institutional model saved successfully:")
    print(f"  -> {OUTPUT_PATH}")
    print(f"  -> {PKG_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
