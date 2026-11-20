
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

DIRECTIONAL_TRENDS = ("BULLISH", "BEARISH")


def _last_valid(values: list) -> Optional[float]:
    for value in reversed(values):
        if value is not None:
            return float(value)
    return None


def _empty_timeframe_result(timeframe: str) -> dict:
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


def analyze_single_timeframe(
    candles: list[Candle],
    timeframe: str,
) -> dict:
    """
    Analyze one timeframe.

    This function does not generate BUY or SELL signals.
    Only closed, validated market data should be supplied.
    """

    if not candles:
        return _empty_timeframe_result(timeframe)

    indicators = calculate_indicators(candles)

    ema20 = _last_valid(indicators["ema_20"])
    ema50 = _last_valid(indicators["ema_50"])
    ema200 = _last_valid(indicators["ema_200"])

    rsi_value = _last_valid(indicators["rsi_14"])
    adx_value = _last_valid(indicators["adx"]["adx"])

    supertrend_direction = _last_valid(
        indicators["supertrend"]["direction"]
    )

    structure = analyze_market_structure(candles)
    structure_trend = structure["trend"]

    trend = "UNKNOWN"
    reasons = []

    # EMA directional classification
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
    else:
        reasons.append("EMA data incomplete")

    # Supertrend is a confirmation, not a replacement for EMA trend.
    if supertrend_direction == 1:
        reasons.append("Supertrend bullish")
    elif supertrend_direction == -1:
        reasons.append("Supertrend bearish")
    else:
        reasons.append("Supertrend unavailable")

    # Market structure context
    if structure_trend != "UNKNOWN":
        reasons.append(f"Market structure: {structure_trend}")
    else:
        reasons.append("Market structure unavailable")

    strength = (
        min(max(adx_value, 0.0), 100.0)
        if adx_value is not None
        else 0.0
    )

    return {
        "timeframe": timeframe,
        "trend": trend,
        "strength": round(strength, 2),
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
    Combine configured timeframe analyses.

    D1, H4 and H1 must agree for higher confirmation.
    M30 must agree for lower confirmation.
    M15 is checked separately by the signal engine.

    Alignment is measured against the total configured weight.
    Missing or UNKNOWN timeframes do not inflate alignment.
    """

    timeframe_results = {}

    for timeframe in TIMEFRAMES:
        candles = candles_by_timeframe.get(timeframe, [])

        timeframe_results[timeframe] = analyze_single_timeframe(
            candles,
            timeframe,
        )

    bullish_score = 0.0
    bearish_score = 0.0
    available_weight = 0.0
    total_weight = sum(
        TIMEFRAME_WEIGHTS.get(timeframe, 1.0)
        for timeframe in TIMEFRAMES
    )

    for timeframe, result in timeframe_results.items():
        weight = TIMEFRAME_WEIGHTS.get(timeframe, 1.0)
        trend = result["trend"]

        if trend not in DIRECTIONAL_TRENDS:
            continue

        available_weight += weight

        if trend == "BULLISH":
            bullish_score += weight
        else:
            bearish_score += weight

    if bullish_score == 0.0 and bearish_score == 0.0:
        overall_trend = "UNKNOWN"
        alignment = 0.0

    elif bullish_score > bearish_score:
        overall_trend = "BULLISH"
        alignment = (
            bullish_score / total_weight * 100.0
            if total_weight
            else 0.0
        )

    elif bearish_score > bullish_score:
        overall_trend = "BEARISH"
        alignment = (
            bearish_score / total_weight * 100.0
            if total_weight
            else 0.0
        )

    else:
        overall_trend = "RANGING"
        alignment = (
            max(bullish_score, bearish_score)
            / total_weight * 100.0
            if total_weight
            else 0.0
        )

    # Higher timeframe agreement: all three must agree.
    higher_timeframes = ("D1", "H4", "H1")

    higher_confirmation = (
        overall_trend in DIRECTIONAL_TRENDS
        and all(
            timeframe in timeframe_results
            and timeframe_results[timeframe]["trend"] == overall_trend
            for timeframe in higher_timeframes
        )
    )

    # M30 confirms the higher-timeframe direction.
    # M15 has its own entry confirmation in signal_engine.py.
    m30_result = timeframe_results.get("M30", {})
    lower_confirmation = (
        overall_trend in DIRECTIONAL_TRENDS
        and m30_result.get("trend") == overall_trend
    )

    return {
        "overall_trend": overall_trend,
        "alignment_percent": round(alignment, 2),
        "bullish_score": round(bullish_score, 2),
        "bearish_score": round(bearish_score, 2),
        "available_weight": round(available_weight, 2),
        "total_weight": round(total_weight, 2),
        "higher_confirmation": higher_confirmation,
        "lower_confirmation": lower_confirmation,
        "timeframes": timeframe_results,
    }
