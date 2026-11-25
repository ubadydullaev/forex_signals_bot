
from typing import Optional

from .config import TIMEFRAMES
from .models import Candle, Signal, AnalysisResult
from .indicators import calculate_indicators
from .market_structure import analyze_market_structure
from .mtf_analysis import analyze_mtf
from .validators import validate_market_data, validate_trade


MINIMUM_CANDLES = 200
MINIMUM_ALIGNMENT = 80.0
MINIMUM_ADX = 15.0
MINIMUM_RR = 1.5
STOP_ATR_MULTIPLIER = 1.5
RECENT_STRUCTURE_CANDLES = 3


def _last_valid(values) -> Optional[float]:
    for value in reversed(values):
        if value is not None:
            return float(value)
    return None


def _hold_signal(symbol, reason, timeframe="M15"):
    return Signal(
        symbol=symbol,
        timeframe=timeframe,
        side="HOLD",
        reason=reason,
    )


def _hold_result(
    symbol,
    reason,
    trend="UNKNOWN",
    strength=0.0,
    reasons=None,
):
    signal = _hold_signal(symbol, reason)

    return AnalysisResult(
        symbol=symbol,
        timeframe="M15",
        signal=signal,
        trend=trend,
        strength=strength,
        reasons=tuple(reasons or (reason,)),
    )


def _get_macd_direction(macd_data):
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
    direction,
    m15_indicators,
    structure,
    m15_candles,
):
    """
    Barcha M15 filtrlarini tekshiradi.
    Birinchi xatoda to'xtamaydi.
    Signal qoidalari avvalgidek: barcha majburiy filtrlar o'tishi kerak.
    """
    failures = []
    confirmations = []

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

    # 1. Supertrend
    if supertrend_direction != expected_supertrend:
        failures.append(
            "M15 Supertrend does not confirm direction"
        )
    else:
        confirmations.append("M15 Supertrend confirmed")

    # 2. RSI
    if rsi_value is None:
        failures.append("RSI data unavailable")
    elif direction == "BULLISH":
        if not 50.0 < rsi_value < 75.0:
            failures.append("RSI does not confirm BUY")
        else:
            confirmations.append(
                f"RSI confirmation: {rsi_value:.2f}"
            )
    else:
        if not 25.0 < rsi_value < 50.0:
            failures.append("RSI does not confirm SELL")
        else:
            confirmations.append(
                f"RSI confirmation: {rsi_value:.2f}"
            )

    # 3. ADX — minimum 15
    if adx_value is None or adx_value < MINIMUM_ADX:
        value_text = (
            "unavailable"
            if adx_value is None
            else f"{adx_value:.2f}"
        )
        failures.append(
            f"ADX trend strength is insufficient "
            f"(M15 ADX={value_text}, minimum={MINIMUM_ADX:.2f})"
        )
    else:
        confirmations.append(
            f"ADX confirmation: {adx_value:.2f}"
        )

    # 4. MACD — neutral accepted, opposite rejected
    if macd_direction == "UNKNOWN":
        failures.append("MACD data unavailable")
    elif macd_direction not in (direction, "NEUTRAL"):
        failures.append("MACD opposes signal direction")
    elif macd_direction == direction:
        confirmations.append("MACD confirmed")
    else:
        confirmations.append("MACD neutral — accepted")

    # 5. Market structure / recent BOS-CHOCH
    structure_trend = structure["trend"]

    if structure_trend == direction:
        confirmations.append(
            "Market structure trend confirmed"
        )
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
            failures.append(
                "No recent BOS/CHOCH confirmation"
            )
        else:
            confirmations.append(
                "Recent BOS/CHOCH confirmed"
            )

    # 6. Liquidity sweep — qo'shimcha dalil, majburiy emas
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
        confirmations.append(
            "Recent liquidity sweep confirmed"
        )

    if failures:
        return False, failures

    return True, confirmations


def analyze_symbol(
    symbol: str,
    candles_by_timeframe: dict[str, list[Candle]],
) -> AnalysisResult:

    # 1. Ma'lumotlarni tekshirish — avvalgidek majburiy
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

    # 2. MTF tahlil
    mtf = analyze_mtf(candles_by_timeframe)

    overall_trend = mtf["overall_trend"]
    alignment = mtf["alignment_percent"]

    # Yo'nalish noma'lum bo'lsa M15 BUY/SELL filtrlarini
    # yo'nalishsiz tekshirish mumkin emas.
    if overall_trend not in ("BULLISH", "BEARISH"):
        return _hold_result(
            symbol,
            "No clear multi-timeframe direction",
            trend=overall_trend,
            strength=alignment,
        )

    # MTF to'siqlarini yig'amiz, lekin shu yerda to'xtamaymiz.
    blockers = []

    if alignment < MINIMUM_ALIGNMENT:
        blockers.append(
            f"MTF alignment too low: "
            f"{alignment:.2f}% "
            f"(minimum {MINIMUM_ALIGNMENT:.2f}%)"
        )

    if not mtf["higher_confirmation"]:
        blockers.append(
            "D1, H4 and H1 do not fully agree"
        )

    # M30 tasdig'i majburiyligicha qoladi.
    if not mtf["lower_confirmation"]:
        blockers.append(
            "M30 does not confirm the higher-timeframe direction"
        )

    # 3. M15 indikatorlari — MTF bloklangan bo'lsa ham tekshiriladi.
    m15_candles = candles_by_timeframe["M15"]

    indicators = calculate_indicators(m15_candles)
    structure = analyze_market_structure(m15_candles)

    direction = overall_trend
    side = "BUY" if direction == "BULLISH" else "SELL"

    # 4. Barcha M15 filtrlarini tekshirish
    confirmed, confirmation_reasons = _check_direction(
        direction=direction,
        m15_indicators=indicators,
        structure=structure,
        m15_candles=m15_candles,
    )

    if not confirmed:
        blockers.extend(confirmation_reasons)

    # Barcha MTF va M15 to'siqlari bitta natijada qaytariladi.
    if blockers:
        reason = "; ".join(blockers)

        return _hold_result(
            symbol,
            reason,
            trend=overall_trend,
            strength=alignment,
            reasons=tuple(blockers),
        )

    # 5. Entry, SL va TP — o'zgarishsiz
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

    # 6. Savdo tekshiruvi — o'zgarishsiz
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

    # 7. Ishonchlilik ko'rsatkichi — o'zgarishsiz
    adx_for_confidence = (
        _last_valid(indicators["adx"]["adx"]) or 0.0
    )

    confidence = min(
        85.0,
        alignment * 0.7
        + min(adx_for_confidence, 50.0) * 0.6,
    )

    # 8. Yakuniy signal
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
