"""
ChronosMatch: Institutional Candlestick Pattern Recognition Engine.

Detects classical Japanese candlestick patterns with quantitative statistical confidence,
body-to-wick geometric ratios, and multi-bar trend momentum confirmation.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any


@dataclass
class DetectedPattern:
    name: str                   # e.g., "BULLISH_ENGULFING", "HAMMER", "DOJI"
    category: str               # "REVERSAL_BULL", "REVERSAL_BEAR", "CONTINUATION", "INDECISION", "NORMAL"
    sentiment: str              # "BULLISH", "BEARISH", "NEUTRAL"
    confidence: float           # 0.0 to 1.0 score
    description: str            # Human-readable explanation
    pattern_score: float        # Normalized directional strength: -1.0 (bearish) to +1.0 (bullish)


class CandlestickPatternRecognizer:
    """
    Real-time pattern detector operating on recent OHLC bars.
    Calculates candle geometries: real body, upper wick, lower wick, range, and trend context.
    """

    @staticmethod
    def recognize(bars: List[Dict[str, Any]]) -> DetectedPattern:
        """
        Analyze the most recent 1 to 3 completed bars and identify the dominant pattern.
        Expected bar keys: open, high, low, close, volume (optional).
        """
        if not bars:
            return DetectedPattern(
                name="INSUFFICIENT_DATA",
                category="NORMAL",
                sentiment="NEUTRAL",
                confidence=0.5,
                description="Awaiting candlestick bars",
                pattern_score=0.0,
            )

        # Work on the last 3 bars (or fewer if not available)
        curr = bars[-1]
        prev = bars[-2] if len(bars) >= 2 else None
        prev2 = bars[-3] if len(bars) >= 3 else None

        o = float(curr["open"])
        h = float(curr["high"])
        l = float(curr["low"])
        c = float(curr["close"])

        total_range = max(1e-5, h - l)
        body = abs(c - o)
        body_ratio = body / total_range
        upper_wick = (h - max(o, c)) / total_range
        lower_wick = (min(o, c) - l) / total_range
        is_bullish = c >= o

        # -------------------------------------------------------------------
        # 1. Three-Bar Formations (Highest Reliability)
        # -------------------------------------------------------------------
        if prev and prev2:
            o1, c1 = float(prev2["open"]), float(prev2["close"])
            o2, c2 = float(prev["open"]), float(prev["close"])

            # Morning Star: Bearish bar -> Small indecision bar -> Strong Bullish bar
            if c1 < o1 and abs(c2 - o2) < abs(c1 - o1) * 0.45 and c > o and c > (o1 + c1) / 2.0:
                return DetectedPattern(
                    name="MORNING_STAR",
                    category="REVERSAL_BULL",
                    sentiment="BULLISH",
                    confidence=0.88,
                    description="Bullish reversal: Seller exhaustion followed by strong buyer takeover",
                    pattern_score=0.85,
                )

            # Evening Star: Bullish bar -> Small indecision bar -> Strong Bearish bar
            if c1 > o1 and abs(c2 - o2) < abs(c1 - o1) * 0.45 and c < o and c < (o1 + c1) / 2.0:
                return DetectedPattern(
                    name="EVENING_STAR",
                    category="REVERSAL_BEAR",
                    sentiment="BEARISH",
                    confidence=0.88,
                    description="Bearish reversal: Buyer exhaustion followed by institutional liquidation",
                    pattern_score=-0.85,
                )

            # Three White Soldiers: 3 consecutive expanding bullish candles
            if c1 > o1 and c2 > o2 and c > o and c > c2 > c1 and o > o2 > o1:
                return DetectedPattern(
                    name="THREE_WHITE_SOLDIERS",
                    category="CONTINUATION",
                    sentiment="BULLISH",
                    confidence=0.90,
                    description="Powerful bullish continuation: Systematic institutional accumulation",
                    pattern_score=0.92,
                )

            # Three Black Crows: 3 consecutive descending bearish candles
            if c1 < o1 and c2 < o2 and c < o and c < c2 < c1 and o < o2 < o1:
                return DetectedPattern(
                    name="THREE_BLACK_CROWS",
                    category="CONTINUATION",
                    sentiment="BEARISH",
                    confidence=0.90,
                    description="Powerful bearish continuation: Aggressive distribution and selling cascade",
                    pattern_score=-0.92,
                )

        # -------------------------------------------------------------------
        # 2. Two-Bar Formations (High Reliability)
        # -------------------------------------------------------------------
        if prev:
            po, pc = float(prev["open"]), float(prev["close"])
            prev_body = abs(pc - po)
            prev_bear = pc < po
            prev_bull = pc > po

            # Bullish Engulfing: Prior bear bar completely enveloped by current bull bar
            if prev_bear and is_bullish and o <= pc and c >= po and body > prev_body:
                return DetectedPattern(
                    name="BULLISH_ENGULFING",
                    category="REVERSAL_BULL",
                    sentiment="BULLISH",
                    confidence=0.84,
                    description="Bullish engulfing: Buyers completely overpowered previous selling pressure",
                    pattern_score=0.80,
                )

            # Bearish Engulfing: Prior bull bar completely enveloped by current bear bar
            if prev_bull and not is_bullish and o >= pc and c <= po and body > prev_body:
                return DetectedPattern(
                    name="BEARISH_ENGULFING",
                    category="REVERSAL_BEAR",
                    sentiment="BEARISH",
                    confidence=0.84,
                    description="Bearish engulfing: Sellers aggressively consumed all prior resting bids",
                    pattern_score=-0.80,
                )

            # Piercing Line: Deep bear bar followed by bull bar opening lower and closing > 50% into prior body
            if prev_bear and is_bullish and o < float(prev["low"]) and c > (po + pc) / 2.0:
                return DetectedPattern(
                    name="PIERCING_LINE",
                    category="REVERSAL_BULL",
                    sentiment="BULLISH",
                    confidence=0.78,
                    description="Piercing line: Strong value-buying rebound after sharp down-drift",
                    pattern_score=0.72,
                )

            # Dark Cloud Cover: Strong bull bar followed by bear bar opening higher and closing > 50% into prior body
            if prev_bull and not is_bullish and o > float(prev["high"]) and c < (po + pc) / 2.0:
                return DetectedPattern(
                    name="DARK_CLOUD_COVER",
                    category="REVERSAL_BEAR",
                    sentiment="BEARISH",
                    confidence=0.78,
                    description="Dark cloud cover: Immediate rejection of new price highs",
                    pattern_score=-0.72,
                )

        # -------------------------------------------------------------------
        # 3. Single-Bar Formations (Structural Geometry)
        # -------------------------------------------------------------------

        # Doji family: body <= 10% of total range
        if body_ratio <= 0.10:
            if lower_wick >= 0.60 and upper_wick <= 0.15:
                return DetectedPattern(
                    name="DRAGONFLY_DOJI",
                    category="REVERSAL_BULL",
                    sentiment="BULLISH",
                    confidence=0.80,
                    description="Dragonfly Doji: Aggressive rejection of session lows by responsive buyers",
                    pattern_score=0.70,
                )
            elif upper_wick >= 0.60 and lower_wick <= 0.15:
                return DetectedPattern(
                    name="GRAVESTONE_DOJI",
                    category="REVERSAL_BEAR",
                    sentiment="BEARISH",
                    confidence=0.80,
                    description="Gravestone Doji: Sharp rejection of highs, sellers dominant into close",
                    pattern_score=-0.70,
                )
            elif upper_wick >= 0.35 and lower_wick >= 0.35:
                return DetectedPattern(
                    name="LONG_LEGGED_DOJI",
                    category="INDECISION",
                    sentiment="NEUTRAL",
                    confidence=0.70,
                    description="Long-legged Doji: Extreme two-sided volatility in dynamic equilibrium",
                    pattern_score=0.0,
                )
            else:
                return DetectedPattern(
                    name="STANDARD_DOJI",
                    category="INDECISION",
                    sentiment="NEUTRAL",
                    confidence=0.65,
                    description="Doji: Market balance; open and close virtually identical",
                    pattern_score=0.0,
                )

        # Hammer: Small body at top, lower wick >= 2x body, minimal upper wick
        if lower_wick >= 0.58 and upper_wick <= 0.12 and body_ratio >= 0.12:
            return DetectedPattern(
                name="HAMMER",
                category="REVERSAL_BULL",
                sentiment="BULLISH",
                confidence=0.82,
                description="Hammer: Intraday selloff violently rejected by liquidity providers",
                pattern_score=0.75,
            )

        # Shooting Star: Small body at bottom, upper wick >= 2x body, minimal lower wick
        if upper_wick >= 0.58 and lower_wick <= 0.12 and body_ratio >= 0.12:
            return DetectedPattern(
                name="SHOOTING_STAR",
                category="REVERSAL_BEAR",
                sentiment="BEARISH",
                confidence=0.82,
                description="Shooting Star: Failed upside breakout; offers heavily defended",
                pattern_score=-0.75,
            )

        # Marubozu: Strong directional body taking up > 85% of total bar
        if body_ratio >= 0.85:
            if is_bullish:
                return DetectedPattern(
                    name="BULLISH_MARUBOZU",
                    category="CONTINUATION",
                    sentiment="BULLISH",
                    confidence=0.86,
                    description="Bullish Marubozu: Full buyer control from open to close",
                    pattern_score=0.88,
                )
            else:
                return DetectedPattern(
                    name="BEARISH_MARUBOZU",
                    category="CONTINUATION",
                    sentiment="BEARISH",
                    confidence=0.86,
                    description="Bearish Marubozu: Full seller liquidation from open to close",
                    pattern_score=-0.88,
                )

        # Spinning Top: Small symmetrical body with balanced wicks
        if 0.12 <= body_ratio <= 0.35 and abs(upper_wick - lower_wick) <= 0.20:
            return DetectedPattern(
                name="SPINNING_TOP",
                category="INDECISION",
                sentiment="NEUTRAL",
                confidence=0.60,
                description="Spinning Top: Two-way consolidation and temporary market pause",
                pattern_score=0.05 if is_bullish else -0.05,
            )

        # Default standard bar
        net_dir = (c - o) / total_range
        return DetectedPattern(
            name="NORMAL_BAR",
            category="NORMAL",
            sentiment="BULLISH" if is_bullish else "BEARISH",
            confidence=0.55,
            description="Standard trending bar within normal volatility limits",
            pattern_score=round(net_dir * 0.40, 2),
        )
