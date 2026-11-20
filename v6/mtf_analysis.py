
from typing import Optional

from .config import TIMEFRAMES
from .models import Candle
from .indicators import calculate_indicators
from .market_structure import analyze_market_structure


# ==========================================
# V6 MULTI-TIMEFRAME ANALYSIS
# ==========================================

TIMEFRAME_WEIGHTS = {
    "D1": 3.0,
    "H4": 3.0,
    "H1": 2.0,
    "M30": 1.0,
    "M15": 0.5,
}


def _last_valid(values: list) -> Optional[float]:
    """Return the latest non-None value."""

    for value in reversed(values):
        if value is not None:
            return float(value)

    return None


def analyze_single_timeframe(
    candles: list[Candle],
    timeframe: str,
) -> dict:
    """
    Analyze one timeframe.

    This function expects closed candles only.
    It does not generate BUY or SELL signals.
    """

    if not candles:
        return {
            "timeframe": timeframe,
            "trend": "UNKNOWN",
            "strength": 0.0,
            "rsi": None,
            "adx": None,
            "ema_20": None,
            "ema_50": None,
            "ema_200": None,
            "supertrend_direction": None,
            "structure_trend": "UNKNOWN",
            "reasons": ["No candle data"],
        }

    indicators = calculate_indicators(candles)

    ema20 = _last_valid(indicators["ema_20"])
    ema50 = _last_valid(indicators["ema_50"])
    ema200 = _last_valid(indicators["ema_200"])

    rsi_value = _last_valid(indicators["rsi_14"])

    adx_data = indicators["adx"]
    adx_value = _last_valid(adx_data["adx"])

    supertrend_data = indicators["supertrend"]
    supertrend_direction = _last_valid(
        supertrend_data["direction"]
    )

    structure = analyze_market_structure(candles)
    structure_trend = structure["trend"]

    trend = "UNKNOWN"
    reasons = []

    # EMA trend classification
    if (
        ema20 is not None
        and ema50 is not None
        and ema200 is not None
    ):
        if ema20 > ema50 > ema200:
            trend = "BULLISH"
            reasons.append("EMA bullish alignment")

        elif ema20 < ema50 < ema200:
            trend = "BEARISH"
            reasons.append("EMA bearish alignment")

        else:
            trend = "RANGING"
            reasons.append("EMA alignment is mixed")

    # Supertrend confirmation
    if supertrend_direction == 1:
        reasons.append("Supertrend bullish")
    elif supertrend_direction == -1:
        reasons.append("Supertrend bearish")

    # Market structure confirmation
    if structure_trend != "UNKNOWN":
        reasons.append(
            f"Market structure: {structure_trend}"
        )

    # Trend strength
    strength = 0.0

    if adx_value is not None:
        strength = min(adx_value, 100.0)

    return {
        "timeframe": timeframe,
        "trend": trend,
        "strength": strength,
        "rsi": rsi_value,
        "adx": adx_value,
        "ema_20": ema20,
        "ema_50": ema50,
        "ema_200": ema200,
        "supertrend_direction": supertrend_direction,
        "structure_trend": structure_trend,
        "reasons": reasons,
    }


def analyze_mtf(
    candles_by_timeframe: dict[str, list[Candle]],
) -> dict:
    """
    Combine D1, H4, H1, M30 and M15 analysis.

    Higher timeframes have greater directional weight.
    M30 and M15 are used as additional confirmations.

    This function does not create a trading signal.
    """

    timeframe_results = {}

    # Analyze every configured timeframe
    for timeframe in TIMEFRAMES:
        candles = candles_by_timeframe.get(
            timeframe,
            []
        )

        timeframe_results[timeframe] = (
            analyze_single_timeframe(
                candles,
                timeframe,
            )
        )

    # Calculate weighted directional score
    bullish_score = 0.0
    bearish_score = 0.0
    available_weight = 0.0

    for timeframe, result in timeframe_results.items():

        weight = TIMEFRAME_WEIGHTS.get(
            timeframe,
            1.0
        )

        trend = result["trend"]

        if trend == "UNKNOWN":
            continue

        available_weight += weight

        if trend == "BULLISH":
            bullish_score += weight

        elif trend == "BEARISH":
            bearish_score += weight

    total_directional_score = (
        bullish_score + bearish_score
    )

    if total_directional_score == 0:
        overall_trend = "UNKNOWN"
        alignment = 0.0

    elif bullish_score > bearish_score:
        overall_trend = "BULLISH"
        alignment = (
            bullish_score / total_directional_score
        ) * 100.0

    elif bearish_score > bullish_score:
        overall_trend = "BEARISH"
        alignment = (
            bearish_score / total_directional_score
        ) * 100.0

    else:
        overall_trend = "RANGING"
        alignment = 50.0

    # Higher timeframe agreement
    higher_timeframes = ["D1", "H4", "H1"]

    higher_trends = [
        timeframe_results[tf]["trend"]
        for tf in higher_timeframes
        if tf in timeframe_results
    ]

    higher_confirmation = (
        len(higher_trends) == 3
        and all(
            trend == overall_trend
            for trend in higher_trends
        )
        and overall_trend in ("BULLISH", "BEARISH")
    )

    # Lower timeframe confirmation
    lower_timeframes = ["M30", "M15"]

    lower_trends = [
        timeframe_results[tf]["trend"]
        for tf in lower_timeframes
        if tf in timeframe_results
    ]

    lower_confirmation = (
        len(lower_trends) == 2
        and all(
            trend == overall_trend
            for trend in lower_trends
        )
        and overall_trend in ("BULLISH", "BEARISH")
    )

    return {
        "overall_trend": overall_trend,
        "alignment_percent": round(alignment, 2),
        "bullish_score": round(bullish_score, 2),
        "bearish_score": round(bearish_score, 2),
        "available_weight": round(available_weight, 2),
        "higher_confirmation": higher_confirmation,
        "lower_confirmation": lower_confirmation,
        "timeframes": timeframe_results,
          }
  
