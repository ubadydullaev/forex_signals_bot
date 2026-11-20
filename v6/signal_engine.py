
from typing import Optional

from .config import TIMEFRAMES
from .models import Candle, Signal, AnalysisResult
from .indicators import calculate_indicators
from .market_structure import analyze_market_structure
from .mtf_analysis import analyze_mtf
from .validators import (
    validate_market_data,
    validate_trade,
)


# ==========================================
# V6 SIGNAL ENGINE
# ==========================================

MINIMUM_CANDLES = 200
MINIMUM_ALIGNMENT = 95.0
MINIMUM_ADX = 18.0
MINIMUM_RR = 1.5
STOP_ATR_MULTIPLIER = 1.5
RECENT_STRUCTURE_CANDLES = 3


def _last_valid(values) -> Optional[float]:
    for value in reversed(values):
        if value is not None:
            return float(value)
    return None


def _hold_signal(
    symbol: str,
    reason: str,
    timeframe: str = "M15",
) -> Signal:
    return Signal(
        symbol=symbol,
        timeframe=timeframe,
        side="HOLD",
        reason=reason,
    )


def _hold_result(
    symbol: str,
    reason: str,
    trend: str = "UNKNOWN",
    strength: float = 0.0,
    reasons: Optional[tuple[str, ...]] = None,
) -> AnalysisResult:
    signal = _hold_signal(symbol, reason)

    return AnalysisResult(
        symbol=symbol,
        timeframe="M15",
        signal=signal,
        trend=trend,
        strength=strength,
        reasons=reasons or (reason,),
    )


def _get_macd_direction(macd_data: dict) -> str:
    macd_line = _last_valid(macd_data["macd"])
    signal_line = _last_valid(macd_data["signal"])
    histogram = _last_valid(macd_data["histogram"])

    if (
        macd_line is None
        or signal_line is None
        or histogram is None
    ):
        return "UNKNOWN"

    if macd_line > signal_line and histogram > 0:
        return "BULLISH"

    if macd_line < signal_line and histogram < 0:
        return "BEARISH"

    return "NEUTRAL"


def _check_direction(
    direction: str,
    m15_indicators: dict,
    structure: dict,
    m15_candles: list[Candle],
) -> tuple[bool, list[str]]:

    reasons = []

    if direction not in ("BULLISH", "BEARISH"):
        return False, ["No clear market direction"]

    expected_supertrend = (
        1 if direction == "BULLISH" else -1
    )

    supertrend_direction = _last_valid(
        m15_indicators["supertrend"]["direction"]
    )

    rsi_value = _last_valid(
        m15_indicators["rsi_14"]
    )

    adx_value = _last_valid(
        m15_indicators["adx"]["adx"]
    )

    macd_direction = _get_macd_direction(
        m15_indicators["macd"]
    )

    # Supertrend
    if supertrend_direction != expected_supertrend:
        return False, [
            "M15 Supertrend does not confirm direction"
        ]

    reasons.append("M15 Supertrend confirmed")

    # RSI
    if rsi_value is None:
        return False, ["RSI data unavailable"]

    if direction == "BULLISH":
        if not 50.0 < rsi_value < 75.0:
            return False, ["RSI does not confirm BUY"]
    else:
        if not 25.0 < rsi_value < 50.0:
            return False, ["RSI does not confirm SELL"]

    reasons.append(f"RSI confirmation: {rsi_value:.2f}")

    # ADX
    if adx_value is None or adx_value < MINIMUM_ADX:
        return False, ["ADX trend strength is insufficient"]

    reasons.append(f"ADX confirmation: {adx_value:.2f}")

    # MACD
    if macd_direction != direction:
        return False, ["MACD does not confirm direction"]

    reasons.append("MACD confirmed")

    # Market structure
    structure_trend = structure["trend"]

    if structure_trend == direction:
        reasons.append("Market structure trend confirmed")
    else:
        recent_timestamps = {
            candle.timestamp
            for candle in m15_candles[-RECENT_STRUCTURE_CANDLES:]
        }

        recent_events = [
            event
            for event in structure["events"]
            if (
                event.timestamp in recent_timestamps
                and event.direction == direction
            )
        ]

        if not recent_events:
            return False, [
                "No recent BOS/CHOCH confirmation"
            ]

        reasons.append("Recent BOS/CHOCH confirmed")

    # Liquidity sweep is optional additional evidence.
    recent_timestamps = {
        candle.timestamp
        for candle in m15_candles[-RECENT_STRUCTURE_CANDLES:]
    }

    recent_sweeps = [
        sweep
        for sweep in structure["liquidity_sweeps"]
        if (
            sweep.timestamp in recent_timestamps
            and sweep.direction == direction
        )
    ]

    if recent_sweeps:
        reasons.append("Recent liquidity sweep confirmed")

    return True, reasons


def analyze_symbol(
    symbol: str,
    candles_by_timeframe: dict[str, list[Candle]],
) -> AnalysisResult:
    """
    Analyze one symbol using all configured timeframes.

    Only fully closed candles should be passed to this function.
    This engine is the only V6 component that decides BUY/SELL/HOLD.
    It does not fetch market data or send Telegram messages.
    """

    # 1. Validate every configured timeframe.
    for timeframe in TIMEFRAMES:
        candles = candles_by_timeframe.get(timeframe, [])

        validation = validate_market_data(
            candles,
            minimum_candles=MINIMUM_CANDLES,
        )

        if not validation.valid:
            reason = (
                f"{timeframe} data rejected: "
                + "; ".join(validation.reasons)
            )

            return _hold_result(
                symbol=symbol,
                reason=reason,
            )

    # 2. Multi-timeframe analysis.
    mtf = analyze_mtf(candles_by_timeframe)

    overall_trend = mtf["overall_trend"]
    alignment = mtf["alignment_percent"]

    if overall_trend not in ("BULLISH", "BEARISH"):
        return _hold_result(
            symbol,
            "No clear multi-timeframe direction",
            trend=overall_trend,
            strength=alignment,
        )

    if alignment < MINIMUM_ALIGNMENT:
        reason = (
            f"MTF alignment too low: "
            f"{alignment:.2f}% (minimum {MINIMUM_ALIGNMENT:.2f}%)"
        )

        return _hold_result(
            symbol,
            reason,
            trend=overall_trend,
            strength=alignment,
        )

    if not mtf["higher_confirmation"]:
        return _hold_result(
            symbol,
            "D1, H4 and H1 do not fully agree",
            trend=overall_trend,
            strength=alignment,
        )

    if not mtf["lower_confirmation"]:
        return _hold_result(
            symbol,
            "M30 does not confirm the higher-timeframe direction",
            trend=overall_trend,
            strength=alignment,
        )

    # 3. M15 indicators and market structure.
    m15_candles = candles_by_timeframe["M15"]

    indicators = calculate_indicators(m15_candles)
    structure = analyze_market_structure(m15_candles)

    direction = overall_trend
    side = "BUY" if direction == "BULLISH" else "SELL"

    # 4. M15 entry confirmations.
    confirmed, confirmation_reasons = _check_direction(
        direction=direction,
        m15_indicators=indicators,
        structure=structure,
        m15_candles=m15_candles,
    )

    if not confirmed:
        reason = "; ".join(confirmation_reasons)

        return _hold_result(
            symbol,
            reason,
            trend=overall_trend,
            strength=alignment,
            reasons=tuple(confirmation_reasons),
        )

    # 5. Entry, stop loss and take profit.
    latest_candle = m15_candles[-1]
    entry = latest_candle.close

    atr_value = _last_valid(indicators["atr_14"])

    if atr_value is None or atr_value <= 0:
        return _hold_result(
            symbol,
            "ATR is unavailable",
            trend=overall_trend,
            strength=alignment,
        )

    stop_distance = atr_value * STOP_ATR_MULTIPLIER

    if side == "BUY":
        stop_loss = entry - stop_distance
        take_profit = entry + stop_distance * MINIMUM_RR
    else:
        stop_loss = entry + stop_distance
        take_profit = entry - stop_distance * MINIMUM_RR

    # 6. Validate the proposed trade.
    validation = validate_trade(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        atr_value=atr_value,
        minimum_rr=MINIMUM_RR,
    )

    if not validation.valid:
        reason = "; ".join(validation.reasons)

        return _hold_result(
            symbol,
            reason,
            trend=overall_trend,
            strength=alignment,
            reasons=tuple(validation.reasons),
        )

    # 7. Confidence is a technical score, not win probability.
    adx_for_confidence = (
        _last_valid(indicators["adx"]["adx"]) or 0.0
    )

    confidence = min(
        95.0,
        alignment * 0.7
        + min(adx_for_confidence, 50.0) * 0.6,
    )

    # 8. Final signal.
    signal = Signal(
        symbol=symbol,
        timeframe="M15",
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_reward=MINIMUM_RR,
        confidence=round(confidence, 2),
        reason="; ".join(confirmation_reasons),
    )

    return AnalysisResult(
        symbol=symbol,
        timeframe="M15",
        signal=signal,
        trend=overall_trend,
        strength=alignment,
        reasons=tuple(confirmation_reasons),
    )
