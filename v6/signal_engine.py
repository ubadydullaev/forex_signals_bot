
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

RECENT_STRUCTURE_CANDLES = 12
RECENT_LIQUIDITY_CANDLES = 3

MACD_MOMENTUM_BARS = 4
MACD_MOMENTUM_CHANGE_THRESHOLD = 0.12

MINIMUM_ENTRY_CONFIRMATIONS = 3


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
    return AnalysisResult(
        symbol=symbol,
        timeframe="M15",
        signal=_hold_signal(symbol, reason),
        trend=trend,
        strength=strength,
        reasons=tuple(reasons or (reason,)),
    )


def _macd_values(macd_data):
    if not isinstance(macd_data, dict):
        return []

    macd_values = macd_data.get("macd", [])
    signal_values = macd_data.get("signal", [])
    histogram_values = macd_data.get("histogram", [])

    if not all(
        isinstance(values, (list, tuple))
        for values in (macd_values, signal_values, histogram_values)
    ):
        return []

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

        try:
            rows.append((
                float(main),
                float(signal),
                float(histogram),
            ))
        except (TypeError, ValueError):
            continue

    return rows


def _analyze_macd_momentum(macd_data, direction):
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
            "MACD qarama-qarshi, momentum uchun qiymatlar yetarli emas",
        )

    magnitudes = [
        abs(row[2])
        for row in rows[-MACD_MOMENTUM_BARS:]
    ]

    previous_average = (
        magnitudes[0] + magnitudes[1]
    ) / 2.0

    recent_average = (
        magnitudes[2] + magnitudes[3]
    ) / 2.0

    if previous_average <= 0:
        return (
            "OPPOSING_UNCLEAR",
            "MACD momentum bazasi yetarli emas",
        )

    threshold = MACD_MOMENTUM_CHANGE_THRESHOLD

    weakening_limit = previous_average * (1.0 - threshold)
    strengthening_limit = previous_average * (1.0 + threshold)

    last_bar_weakening = magnitudes[-1] < magnitudes[-2]
    last_bar_strengthening = magnitudes[-1] > magnitudes[-2]

    if recent_average <= weakening_limit and last_bar_weakening:
        change_percent = (
            (previous_average - recent_average)
            / previous_average
            * 100.0
        )

        return (
            "OPPOSING_WEAKENING",
            f"MACD qarama-qarshi, momentum {change_percent:.1f}% susaymoqda",
        )

    if recent_average >= strengthening_limit and last_bar_strengthening:
        change_percent = (
            (recent_average - previous_average)
            / previous_average
            * 100.0
        )

        return (
            "OPPOSING_STRENGTHENING",
            f"MACD qarama-qarshi va momentum {change_percent:.1f}% kuchaymoqda",
        )

    return (
        "OPPOSING_UNCLEAR",
        "MACD qarama-qarshi, momentum o'zgarishi aniq tasdiqlanmadi",
    )


def _check_m30_momentum(direction, m30_indicators):
    if not m30_indicators:
        return True, ["M30 MACD momentum mavjud emas — neytral"]

    macd_data = m30_indicators.get("macd")

    if not isinstance(macd_data, dict):
        return True, ["M30 MACD ma'lumotlari to'liq emas — neytral"]

    state, message = _analyze_macd_momentum(macd_data, direction)

    if state == "OPPOSING_STRENGTHENING":
        return False, [f"M30: {message}"]

    if state == "OPPOSING_WEAKENING":
        return True, [f"M30: {message}"]

    if state == "OPPOSING_UNCLEAR":
        return True, [f"M30: {message} — qo'shimcha kuzatuv"]

    if state == "ALIGNED":
        return True, ["M30 MACD asosiy yo'nalishni tasdiqladi"]

    if state == "UNKNOWN":
        return True, ["M30 MACD noma'lum — neytral"]

    return True, ["M30 MACD neytral"]


def _check_direction(
    direction,
    m15_indicators,
    structure,
    m15_candles,
    m30_indicators=None,
):
    confirmations = []
    observations = []

    if direction not in ("BULLISH", "BEARISH"):
        return False, ["No clear market direction"]

    expected_supertrend = (
        1 if direction == "BULLISH" else -1
    )

    # 1. Supertrend
    supertrend_direction = _last_valid(
        m15_indicators["supertrend"]["direction"]
    )

    if supertrend_direction == expected_supertrend:
        confirmations.append("M15 Supertrend confirmed")
    else:
        observations.append("M15 Supertrend yo'nalishni tasdiqlamadi")

    # 2. RSI
    rsi_value = _last_valid(m15_indicators["rsi_14"])

    if rsi_value is None:
        observations.append("RSI ma'lumotlari mavjud emas")

    elif direction == "BULLISH":
        if 48.0 < rsi_value < 78.0:
            confirmations.append(
                f"RSI BUY confirmation: {rsi_value:.2f}"
            )
        else:
            observations.append(
                f"RSI BUY uchun mos emas: {rsi_value:.2f}"
            )

    else:
        if 22.0 < rsi_value < 52.0:
            confirmations.append(
                f"RSI SELL confirmation: {rsi_value:.2f}"
            )
        else:
            observations.append(
                f"RSI SELL uchun mos emas: {rsi_value:.2f}"
            )

    # 3. ADX
    adx_value = _last_valid(m15_indicators["adx"]["adx"])

    if adx_value is not None and adx_value >= MINIMUM_ADX:
        confirmations.append(
            f"ADX confirmation: {adx_value:.2f}"
        )
    else:
        observations.append(
            f"ADX yetarli emas: {adx_value}"
        )

    # 4. M15 MACD
    macd_state, macd_message = _analyze_macd_momentum(
        m15_indicators["macd"],
        direction,
    )

    # MUHIM: M15 MACD qarama-qarshi tomonga kuchayayotgan bo'lsa,
    # BUY yoki SELL signalini darhol bloklaymiz.
    if macd_state == "OPPOSING_STRENGTHENING":
        return False, [
            f"M15 MACD qarama-qarshi momentum kuchaymoqda: {macd_message}"
        ]

    if macd_state in ("ALIGNED", "OPPOSING_WEAKENING"):
        confirmations.append(macd_message)
    else:
        observations.append(macd_message)

    # 5. Market structure / BOS / CHOCH
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

        if recent_events:
            confirmations.append("Recent BOS/CHOCH confirmed")
        else:
            observations.append(
                "So'nggi M15 shamlarida mos BOS/CHOCH topilmadi"
            )

    # M30 MACD momentum tekshiruvi
    if m30_indicators is not None:
        m30_ok, m30_reasons = _check_m30_momentum(
            direction,
            m30_indicators,
        )

        if not m30_ok:
            return False, m30_reasons

        confirmations.extend(m30_reasons)

    # Likvidlik sweep — qo'shimcha tasdiq
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
        confirmations.append("Recent liquidity sweep confirmed")

    entry_confirmations = [
        item for item in confirmations
        if not item.startswith("M30 ")
    ]

    if len(entry_confirmations) < MINIMUM_ENTRY_CONFIRMATIONS:
        return False, [
            f"M15 tasdiqlari yetarli emas: "
            f"{len(entry_confirmations)}/{MINIMUM_ENTRY_CONFIRMATIONS}",
            *observations,
        ]

    return True, confirmations + observations


def analyze_symbol(
    symbol: str,
    candles_by_timeframe: dict[str, list[Candle]],
) -> AnalysisResult:

    # 1. Ma'lumotlar validatsiyasi
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

            return _hold_result(symbol=symbol, reason=reason)

    # 2. Multi-timeframe tahlil
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
            f"MTF alignment too low: {alignment:.2f}% "
            f"(minimum {MINIMUM_ALIGNMENT:.2f}%)"
        )

    if not mtf["higher_confirmation"]:
        blockers.append(
            "D1, H4 and H1 higher timeframe confirmation insufficient"
        )

    if not mtf["lower_confirmation"]:
        blockers.append(
            "M30 trend does not confirm higher-timeframe direction"
        )

    if blockers:
        return _hold_result(
            symbol,
            "; ".join(blockers),
            trend=overall_trend,
            strength=alignment,
            reasons=tuple(blockers),
        )

    # 3. Indikatorlar
    m15_candles = candles_by_timeframe["M15"]
    m30_candles = candles_by_timeframe["M30"]

    indicators = calculate_indicators(m15_candles)
    m30_indicators = calculate_indicators(m30_candles)

    structure = analyze_market_structure(m15_candles)

    direction = overall_trend
    side = "BUY" if direction == "BULLISH" else "SELL"

    # 4. Kirish tasdiqlari
    confirmed, confirmation_reasons = _check_direction(
        direction=direction,
        m15_indicators=indicators,
        structure=structure,
        m15_candles=m15_candles,
        m30_indicators=m30_indicators,
    )

    if not confirmed:
        return _hold_result(
            symbol,
            "; ".join(confirmation_reasons),
            trend=overall_trend,
            strength=alignment,
            reasons=tuple(confirmation_reasons),
        )

    # 5. Entry, SL va TP
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

    # 6. Savdo validatsiyasi
    validation = validate_trade(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        atr_value=atr_value,
        minimum_rr=MINIMUM_RR,
    )

    if not validation.valid:
        return _hold_result(
            symbol,
            "; ".join(validation.reasons),
            trend=overall_trend,
            strength=alignment,
            reasons=tuple(validation.reasons),
        )

    # 7. Ishonchlilik
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
