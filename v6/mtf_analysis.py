
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
ALIGNMENT_TIMEFRAMES = ("D1", "H4", "H1", "M30")
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
        "supertrend_confirmed": False,
        "structure_trend": "UNKNOWN",
        "reasons": ["No candle data"],
    }


def analyze_single_timeframe(
    candles: list[Candle],
    timeframe: str,
) -> dict:
    """
    EMA 20/50/200 establishes direction.
    Supertrend must confirm the same direction.
    Structure provides additional market context.
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

    ema_trend = "UNKNOWN"
    trend = "UNKNOWN"
    supertrend_confirmed = False
    reasons = []

    # 1. EMA direction
    if all(value is not None for value in (ema20, ema50, ema200)):
        if ema20 > ema50 > ema200:
            ema_trend = "BULLISH"
            reasons.append("EMA bullish alignment")

        elif ema20 < ema50 < ema200:
            ema_trend = "BEARISH"
            reasons.append("EMA bearish alignment")

        else:
            ema_trend = "RANGING"
            reasons.append("EMA alignment is mixed")
    else:
        reasons.append("EMA data incomplete")

    # 2. Supertrend confirmation
    if supertrend_direction is None:
        reasons.append("Supertrend unavailable")

    elif supertrend_direction == 1:
        reasons.append("Supertrend bullish")

        if ema_trend == "BULLISH":
            trend = "BULLISH"
            supertrend_confirmed = True
        elif ema_trend == "BEARISH":
            trend = "RANGING"
            reasons.append("EMA and Supertrend disagree")

    elif supertrend_direction == -1:
        reasons.append("Supertrend bearish")

        if ema_trend == "BEARISH":
            trend = "BEARISH"
            supertrend_confirmed = True
        elif ema_trend == "BULLISH":
            trend = "RANGING"
            reasons.append("EMA and Supertrend disagree")

    else:
        reasons.append("Invalid Supertrend direction")

    if ema_trend == "RANGING":
        trend = "RANGING"

    # 3. Market structure context
    if structure_trend != "UNKNOWN":
        reasons.append(f"Market structure: {structure_trend}")
    else:
        reasons.append("Market structure unavailable")

    # 4. ADX strength
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
        "supertrend_confirmed": supertrend_confirmed,
        "structure_trend": structure_trend,
        "reasons": reasons,
    }


def analyze_mtf(
    candles_by_timeframe: dict[str, list[Candle]],
) -> dict:
    """
    Combine configured timeframe analyses.

    D1, H4 and H1 must agree.
    M30 must confirm the same direction.
    M15 is checked separately by signal_engine.py.

    Alignment uses total configured weight.
    UNKNOWN and RANGING do not inflate alignment.
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
            if total_weight else 0.0
        )

    elif bearish_score > bullish_score:
        overall_trend = "BEARISH"
        alignment = (
            bearish_score / total_weight * 100.0
            if total_weight else 0.0
        )

    else:
        overall_trend = "RANGING"
        alignment = (
            max(bullish_score, bearish_score)
            / total_weight * 100.0
            if total_weight else 0.0
        )

    # Higher timeframe agreement
    higher_timeframes = ("D1", "H4", "H1")

    higher_confirmation = (
        overall_trend in DIRECTIONAL_TRENDS
        and all(
            timeframe_results[timeframe]["trend"] == overall_trend
            for timeframe in higher_timeframes
        )
    )

    # M30 confirmation
    lower_confirmation = (
        overall_trend in DIRECTIONAL_TRENDS
        and timeframe_results.get("M30", {}).get("trend")
        == overall_trend
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
