
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
MINIMUM_ALIGNMENT = 75.0
MINIMUM_ADX = 18.0
MINIMUM_RR = 1.5
STOP_ATR_MULTIPLIER = 1.5


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
    mtf: dict,
    m15_indicators: dict,
    structure: dict,
) -> tuple[bool, list[str]]:
    """
    Check whether indicator and structure confirmations
    support the selected direction.
    """

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

    if supertrend_direction != expected_supertrend:
        return False, ["M15 Supertrend does not confirm direction"]

    reasons.append("M15 Supertrend confirmed")

    if rsi_value is None:
        return False, ["RSI data unavailable"]

    if direction == "BULLISH":
        if not 50.0 < rsi_value < 75.0:
            return False, ["RSI does not confirm BUY"]

    else:
        if not 25.0 < rsi_value < 50.0:
            return False, ["RSI does not confirm SELL"]

    reasons.append(f"RSI confirmation: {rsi_value:.2f}")

    if adx_value is None or adx_value < MINIMUM_ADX:
        return False, ["ADX trend strength is insufficient"]

    reasons.append(f"ADX confirmation: {adx_value:.2f}")

    if macd_direction != direction:
        return False, ["MACD does not confirm direction"]

    reasons.append("MACD confirmed")

    # M15 market structure
    structure_trend = structure["trend"]

    if structure_trend != direction:
        latest_event = (
            structure["events"][-1]
            if structure["events"]
            else None
        )

        if (
            latest_event is None
            or latest_event.direction != direction
        ):
            return False, ["Market structure does not confirm direction"]

    reasons.append("Market structure confirmed")

    # Liquidity sweep is an additional confirmation,
    # not a mandatory condition.
    if structure["liquidity_sweeps"]:
        latest_sweep = structure["liquidity_sweeps"][-1]

        if latest_sweep.direction == direction:
            reasons.append("Liquidity sweep confirmed")

    return True, reasons


def analyze_symbol(
    symbol: str,
    candles_by_timeframe: dict[str, list[Candle]],
) -> AnalysisResult:
    """
    Analyze one symbol using all configured timeframes.

    IMPORTANT:
    Only fully closed candles should be passed to this function.
    This engine does not fetch market data or send Telegram messages.
    """

    # --------------------------------------
    # 1. Check available market data
    # --------------------------------------

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

            signal = _hold_signal(symbol, reason)

            return AnalysisResult(
                symbol=symbol,
                timeframe="M15",
                signal=signal,
                trend="UNKNOWN",
                strength=0.0,
                reasons=(reason,),
            )

    # --------------------------------------
    # 2. Multi-timeframe analysis
    # --------------------------------------

    mtf = analyze_mtf(candles_by_timeframe)

    overall_trend = mtf["overall_trend"]
    alignment = mtf["alignment_percent"]

    if overall_trend not in ("BULLISH", "BEARISH"):
        reason = "No clear multi-timeframe direction"

        signal = _hold_signal(symbol, reason)

        return AnalysisResult(
            symbol=symbol,
            timeframe="M15",
            signal=signal,
            trend=overall_trend,
            strength=alignment,
            reasons=(reason,),
        )

    if alignment < MINIMUM_ALIGNMENT:
        reason = (
            f"MTF alignment too low: "
            f"{alignment:.2f}%"
        )

        signal = _hold_signal(symbol, reason)

        return AnalysisResult(
            symbol=symbol,
            timeframe="M15",
            signal=signal,
            trend=overall_trend,
            strength=alignment,
            reasons=(reason,),
        )

    if not mtf["higher_confirmation"]:
        reason = "D1, H4 and H1 do not fully agree"

        signal = _hold_signal(symbol, reason)

        return AnalysisResult(
            symbol=symbol,
            timeframe="M15",
            signal=signal,
            trend=overall_trend,
            strength=alignment,
            reasons=(reason,),
        )

    if not mtf["lower_confirmation"]:
        reason = "M30 and M15 confirmation is missing"

        signal = _hold_signal(symbol, reason)

        return AnalysisResult(
            symbol=symbol,
            timeframe="M15",
            signal=signal,
            trend=overall_trend,
            strength=alignment,
            reasons=(reason,),
        )

    # --------------------------------------
    # 3. Calculate M15 indicators
    # --------------------------------------

    m15_candles = candles_by_timeframe["M15"]

    indicators = calculate_indicators(m15_candles)

    structure = analyze_market_structure(m15_candles)

    direction = overall_trend

    side = (
        "BUY"
        if direction == "BULLISH"
        else "SELL"
    )

    # --------------------------------------
    # 4. Check confirmations
    # --------------------------------------

    confirmed, confirmation_reasons = _check_direction(
        direction,
        mtf,
        indicators,
        structure,
    )

    if not confirmed:
        reason = "; ".join(confirmation_reasons)

        signal = _hold_signal(symbol, reason)

        return AnalysisResult(
            symbol=symbol,
            timeframe="M15",
            signal=signal,
            trend=overall_trend,
            strength=alignment,
            reasons=tuple(confirmation_reasons),
        )

    # --------------------------------------
    # 5. Calculate entry, SL and TP
    # --------------------------------------

    latest_candle = m15_candles[-1]

    entry = latest_candle.close

    atr_value = _last_valid(
        indicators["atr_14"]
    )

    if atr_value is None or atr_value <= 0:
        reason = "ATR is unavailable"

        signal = _hold_signal(symbol, reason)

        return AnalysisResult(
            symbol=symbol,
            timeframe="M15",
            signal=signal,
            trend=overall_trend,
            strength=alignment,
            reasons=(reason,),
        )

    stop_distance = atr_value * STOP_ATR_MULTIPLIER

    if side == "BUY":
        stop_loss = entry - stop_distance
        take_profit = entry + stop_distance * MINIMUM_RR

    else:
        stop_loss = entry + stop_distance
        take_profit = entry - stop_distance * MINIMUM_RR

    # --------------------------------------
    # 6. Validate trade
    # --------------------------------------

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

        signal = _hold_signal(symbol, reason)

        return AnalysisResult(
            symbol=symbol,
            timeframe="M15",
            signal=signal,
            trend=overall_trend,
            strength=alignment,
            reasons=tuple(validation.reasons),
        )

    # --------------------------------------
    # 7. Calculate confidence estimate
    # --------------------------------------

    confidence = min(
        95.0,
        alignment * 0.7 + min(
            _last_valid(indicators["adx"]["adx"]) or 0.0,
            50.0,
        ) * 0.6,
    )

    # --------------------------------------
    # 8. Create final signal
    # --------------------------------------

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
          
