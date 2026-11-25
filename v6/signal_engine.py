
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

# BOS/CHOCH uchun oxirgi 12 ta yopilgan M15 sham.
RECENT_STRUCTURE_CANDLES = 12

# Likvidlik sweep alohida, avvalgi 3 shamda tekshiriladi.
RECENT_LIQUIDITY_CANDLES = 3

MACD_MOMENTUM_BARS = 4


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


def _macd_values(macd_data):
    """MACD, signal va histogramma qiymatlarini juft holda oladi."""
    macd_values = macd_data.get("macd", [])
    signal_values = macd_data.get("signal", [])
    histogram_values = macd_data.get("histogram", [])

    length = min(
        len(macd_values),
        len(signal_values),
        len(histogram_values),
    )

    rows = []

    for i in range(length):
        main = macd_values[i]
        signal = signal_values[i]
        histogram = histogram_values[i]

        if main is None or signal is None or histogram is None:
            continue

        rows.append((
            float(main),
            float(signal),
            float(histogram),
        ))

    return rows


def _analyze_macd_momentum(macd_data, direction):
    """
    MACD holati va momentumini tekshiradi.

    Opposite MACD faqat oxirgi 4 ta qiymatda histogramma
    ketma-ket kichrayganida susayayotgan deb qabul qilinadi.
    Boshqa noaniq holatlarda signal xavfsizlik uchun HOLD bo'ladi.
    """
    rows = _macd_values(macd_data)

    if not rows:
        return "UNKNOWN", "MACD ma'lumotlari mavjud emas"

    main, signal, histogram = rows[-1]

    if main > signal and histogram > 0:
        macd_direction = "BULLISH"
    elif main < signal and histogram < 0:
        macd_direction = "BEARISH"
    else:
        return "NEUTRAL", "MACD neytral"

    if macd_direction == direction:
        return "ALIGNED", "MACD asosiy yo'nalishni tasdiqladi"

    if len(rows) < MACD_MOMENTUM_BARS:
        return (
            "OPPOSING_UNCLEAR",
            "MACD qarama-qarshi, momentum uchun 4 ta qiymat yetarli emas",
        )

    magnitudes = [
        abs(row[2])
        for row in rows[-MACD_MOMENTUM_BARS:]
    ]

    weakening = all(
        magnitudes[i] > magnitudes[i + 1]
        for i in range(len(magnitudes) - 1)
    )

    if weakening:
        return (
            "OPPOSING_WEAKENING",
            "MACD qarama-qarshi, lekin momentum 3 bosqichda susaymoqda",
        )

    strengthening = (
        magnitudes[-1] > magnitudes[-2]
    )

    if strengthening:
        return (
            "OPPOSING_STRENGTHENING",
            "MACD qarama-qarshi va momentum kuchaymoqda",
        )

    return (
        "OPPOSING_UNCLEAR",
        "MACD qarama-qarshi, momentum susayishi tasdiqlanmadi",
    )


def _get_macd_direction(macd_data):
    """Eski chaqiruvlar bilan moslik uchun saqlangan yordamchi funksiya."""
    rows = _macd_values(macd_data)

    if not rows:
        return "UNKNOWN"

    main, signal, histogram = rows[-1]

    if main > signal and histogram > 0:
        return "BULLISH"

    if main < signal and histogram < 0:
        return "BEARISH"

    return "NEUTRAL"


def _check_direction(
    direction,
    m15_indicators,
    structure,
    m15_candles,
):
    """Barcha M15 filtrlarini tekshiradi va barcha to'siqlarni yig'adi."""
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

    # 3. ADX
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

    # 4. MACD momentum
    macd_state, macd_message = _analyze_macd_momentum(
        m15_indicators["macd"],
        direction,
    )

    if macd_state == "UNKNOWN":
        failures.append("MACD data unavailable")
    elif macd_state == "OPPOSING_STRENGTHENING":
        failures.append(macd_message)
    elif macd_state == "OPPOSING_UNCLEAR":
        failures.append(macd_message)
    elif macd_state == "OPPOSING_WEAKENING":
        confirmations.append(macd_message)
    elif macd_state == "ALIGNED":
        confirmations.append(macd_message)
    else:
        confirmations.append("MACD neutral — accepted")

    # 5. Market structure / BOS-CHOCH
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
                "No matching BOS/CHOCH in last "
                f"{RECENT_STRUCTURE_CANDLES} M15 candles"
            )
        else:
            confirmations.append(
                "Recent BOS/CHOCH confirmed"
            )

    # 6. Liquidity sweep — qo'shimcha tasdiq, majburiy emas
    recent_sweep_timestamps = {
        candle.timestamp
        for candle in m15_candles[-RECENT_LIQUIDITY_CANDLES:]
    }

    recent_sweeps = [
        sweep
        for sweep in structure["liquidity_sweeps"]
        if (
            sweep.timestamp in recent_sweep_timestamps
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

    # 1. Ma'lumotlarni tekshirish
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

    if overall_trend not in ("BULLISH", "BEARISH"):
        return _hold_result(
            symbol,
            "No clear multi-timeframe direction",
            trend=overall_trend,
            strength=alignment,
        )

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

    # M30 majburiy tasdiq bo'lib qoladi.
    if not mtf["lower_confirmation"]:
        blockers.append(
            "M30 does not confirm the higher-timeframe direction"
        )

    # 3. M15 indikatorlari va struktura
    m15_candles = candles_by_timeframe["M15"]

    indicators = calculate_indicators(m15_candles)
    structure = analyze_market_structure(m15_candles)

    direction = overall_trend
    side = "BUY" if direction == "BULLISH" else "SELL"

    # 4. M15 filtrlarining barchasini tekshirish
    confirmed, confirmation_reasons = _check_direction(
        direction=direction,
        m15_indicators=indicators,
        structure=structure,
        m15_candles=m15_candles,
    )

    if not confirmed:
        blockers.extend(confirmation_reasons)

    if blockers:
        reason = "; ".join(blockers)

        return _hold_result(
            symbol,
            reason,
            trend=overall_trend,
            strength=alignment,
            reasons=tuple(blockers),
        )

    # 5. Entry, SL va TP — formulalar o'zgarmagan
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

    # 6. Savdo tekshiruvi
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

    # 7. Ishonchlilik hisoblash formulasi o'zgarmagan
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
