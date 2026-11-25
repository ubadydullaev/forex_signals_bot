from typing import Optional

from .config import TIMEFRAMES
from .models import Candle, Signal, AnalysisResult
from .indicators import calculate_indicators
from .market_structure import analyze_market_structure
from .mtf_analysis import analyze_mtf, analyze_single_timeframe
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

COUNTER_MINIMUM_ADX = 18.0


def _last_valid(values) -> Optional[float]:
    for value in reversed(values):
        if value is not None:
            return float(value)
    return None


def _hold_result(symbol, reason, trend="UNKNOWN", strength=0.0):
    return AnalysisResult(
        symbol=symbol,
        timeframe="M15",
        signal=Signal(
            symbol=symbol,
            timeframe="M15",
            side="HOLD",
            reason=reason,
        ),
        trend=trend,
        strength=strength,
        reasons=(reason,),
    )


def _macd_values(macd_data):
    if not isinstance(macd_data, dict):
        return []

    arrays = (
        macd_data.get("macd", []),
        macd_data.get("signal", []),
        macd_data.get("histogram", []),
    )

    if not all(isinstance(x, (list, tuple)) for x in arrays):
        return []

    length = min(len(x) for x in arrays)
    rows = []

    for i in range(length):
        try:
            values = tuple(float(x[i]) for x in arrays)
            rows.append(values)
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
        return "ALIGNED", "MACD yo'nalishni tasdiqladi"

    if len(rows) < MACD_MOMENTUM_BARS:
        return "OPPOSING_UNCLEAR", "MACD qarama-qarshi, qiymatlar yetarli emas"

    magnitudes = [abs(row[2]) for row in rows[-MACD_MOMENTUM_BARS:]]
    previous = (magnitudes[0] + magnitudes[1]) / 2
    recent = (magnitudes[2] + magnitudes[3]) / 2

    if previous <= 0:
        return "OPPOSING_UNCLEAR", "MACD momentum bazasi yetarli emas"

    threshold = MACD_MOMENTUM_CHANGE_THRESHOLD

    weakening = (
        recent <= previous * (1 - threshold)
        and magnitudes[-1] < magnitudes[-2]
    )

    strengthening = (
        recent >= previous * (1 + threshold)
        and magnitudes[-1] > magnitudes[-2]
    )

    if weakening:
        change = (previous - recent) / previous * 100
        return (
            "OPPOSING_WEAKENING",
            f"Qarama-qarshi MACD momentum {change:.1f}% susaymoqda",
        )

    if strengthening:
        change = (recent - previous) / previous * 100
        return (
            "OPPOSING_STRENGTHENING",
            f"Qarama-qarshi MACD momentum {change:.1f}% kuchaymoqda",
        )

    return "OPPOSING_UNCLEAR", "MACD qarama-qarshi, momentum aniq emas"


def _check_m30_momentum(direction, indicators):
    if not indicators:
        return True, ["M30 MACD neytral"]

    state, message = _analyze_macd_momentum(
        indicators.get("macd"),
        direction,
    )

    if state == "OPPOSING_STRENGTHENING":
        return False, [f"M30 MACD qarama-qarshi kuchaymoqda: {message}"]

    return True, [message]


def _check_direction(direction, indicators, structure, candles, m30_indicators):
    if direction not in ("BULLISH", "BEARISH"):
        return False, ["Aniq yo'nalish topilmadi"]

    confirmations = []
    observations = []

    expected = 1 if direction == "BULLISH" else -1

    # Supertrend
    st = _last_valid(indicators["supertrend"]["direction"])

    if st == expected:
        confirmations.append("M15 Supertrend tasdiqladi")
    else:
        observations.append("M15 Supertrend tasdiqlamadi")

    # RSI
    rsi = _last_valid(indicators["rsi_14"])

    if rsi is not None:
        if direction == "BULLISH" and 48 < rsi < 78:
            confirmations.append(f"RSI BUY: {rsi:.2f}")
        elif direction == "BEARISH" and 22 < rsi < 52:
            confirmations.append(f"RSI SELL: {rsi:.2f}")
        else:
            observations.append(f"RSI mos emas: {rsi:.2f}")
    else:
        observations.append("RSI mavjud emas")

    # ADX
    adx = _last_valid(indicators["adx"]["adx"])

    if adx is not None and adx >= MINIMUM_ADX:
        confirmations.append(f"ADX: {adx:.2f}")
    else:
        observations.append(f"ADX yetarli emas: {adx}")

    # MACD
    macd_state, macd_message = _analyze_macd_momentum(
        indicators["macd"],
        direction,
    )

    # Qarama-qarshi MACD kuchli bo'lsa, aynan shu yo'nalish bloklanadi.
    if macd_state == "OPPOSING_STRENGTHENING":
        return False, [f"MACD qarama-qarshi kuchaymoqda: {macd_message}"]

    if macd_state in ("ALIGNED", "OPPOSING_WEAKENING"):
        confirmations.append(macd_message)
    else:
        observations.append(macd_message)

    # Market structure
    if structure["trend"] == direction:
        confirmations.append("Market structure tasdiqladi")
    else:
        recent_ts = {
            candle.timestamp
            for candle in candles[-RECENT_STRUCTURE_CANDLES:]
        }

        recent_events = [
            event for event in structure["events"]
            if event.timestamp in recent_ts and event.direction == direction
        ]

        if recent_events:
            confirmations.append("Yaqinda BOS/CHOCH tasdiqlandi")
        else:
            observations.append("Mos BOS/CHOCH topilmadi")

    # M30 momentum tasdig'i
    m30_ok, m30_reasons = _check_m30_momentum(
        direction,
        m30_indicators,
    )

    if not m30_ok:
        return False, m30_reasons

    observations.extend(m30_reasons)

    # Liquidity sweep
    recent_ts = {
        candle.timestamp
        for candle in candles[-RECENT_LIQUIDITY_CANDLES:]
    }

    sweeps = [
        sweep for sweep in structure["liquidity_sweeps"]
        if sweep.timestamp in recent_ts and sweep.direction == direction
    ]

    if sweeps:
        confirmations.append("Liquidity sweep tasdiqlandi")

    if len(confirmations) < MINIMUM_ENTRY_CONFIRMATIONS:
        return False, [
            f"Tasdiqlar yetarli emas: {len(confirmations)}/"
            f"{MINIMUM_ENTRY_CONFIRMATIONS}",
            *observations,
        ]

    return True, confirmations + observations


def _build_trade_result(
    symbol,
    direction,
    candles,
    m30_indicators,
    trend_label,
    strength,
    timeframe_label,
):
    indicators = calculate_indicators(candles)
    structure = analyze_market_structure(candles)

    confirmed, reasons = _check_direction(
        direction,
        indicators,
        structure,
        candles,
        m30_indicators,
    )

    if not confirmed:
        return None, reasons

    entry = candles[-1].close
    atr = _last_valid(indicators["atr_14"])

    if atr is None or atr <= 0:
        return None, ["ATR mavjud emas yoki noto'g'ri"]

    side = "BUY" if direction == "BULLISH" else "SELL"
    distance = atr * STOP_ATR_MULTIPLIER

    if side == "BUY":
        sl = entry - distance
        tp = entry + distance * MINIMUM_RR
    else:
        sl = entry + distance
        tp = entry - distance * MINIMUM_RR

    validation = validate_trade(
        side=side,
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        atr_value=atr,
        minimum_rr=MINIMUM_RR,
    )

    if not validation.valid:
        return None, validation.reasons

    adx = _last_valid(indicators["adx"]["adx"]) or 0.0
    confidence = min(85.0, strength * 0.7 + min(adx, 50.0) * 0.6)

    signal = Signal(
        symbol=symbol,
        timeframe=timeframe_label,
        side=side,
        entry=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward=MINIMUM_RR,
        confidence=round(confidence, 2),
        reason="; ".join(reasons),
    )

    return AnalysisResult(
        symbol=symbol,
        timeframe=timeframe_label,
        signal=signal,
        trend=trend_label,
        strength=round(strength, 2),
        reasons=tuple(reasons),
    ), reasons


def analyze_symbol(symbol, candles_by_timeframe):
    """
    Natija: AnalysisResult obyektlari ro'yxati.
    Bir instrumentdan MAIN va COUNTER-TREND signallari chiqishi mumkin.
    """

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
            return [_hold_result(symbol, reason)]

    mtf = analyze_mtf(candles_by_timeframe)

    overall = mtf["overall_trend"]
    alignment = mtf["alignment_percent"]

    if overall not in ("BULLISH", "BEARISH"):
        return [
            _hold_result(
                symbol,
                "Katta timeframe'larda aniq yo'nalish yo'q",
                overall,
                alignment,
            )
        ]

    m15 = candles_by_timeframe["M15"]
    m30 = candles_by_timeframe["M30"]
    m30_indicators = calculate_indicators(m30)

    results = []

    # -------------------------------------------------
    # 1. MAIN SIGNAL — katta timeframe yo'nalishi
    # -------------------------------------------------
    main_blockers = []

    if alignment < MINIMUM_ALIGNMENT:
        main_blockers.append(
            f"MTF mosligi past: {alignment:.2f}%"
        )

    if not mtf["higher_confirmation"]:
        main_blockers.append(
            "D1/H4/H1 tasdig'i yetarli emas"
        )

    if not mtf["lower_confirmation"]:
        main_blockers.append(
            "M30 katta timeframe yo'nalishini tasdiqlamadi"
        )

    if main_blockers:
        results.append(
            _hold_result(
                symbol,
                "; ".join(main_blockers),
                overall,
                alignment,
            )
        )
    else:
        main_result, main_reasons = _build_trade_result(
            symbol=symbol,
            direction=overall,
            candles=m15,
            m30_indicators=m30_indicators,
            trend_label=overall,
            strength=alignment,
            timeframe_label="M15",
        )

        if main_result:
            main_result.signal.reason = (
                "MAIN SIGNAL | " + main_result.signal.reason
            )
            results.append(main_result)
        else:
            results.append(
                _hold_result(
                    symbol,
                    "MAIN signal tasdiqlanmadi: "
                    + "; ".join(main_reasons),
                    overall,
                    alignment,
                )
            )

    # -------------------------------------------------
    # 2. COUNTER-TREND — M15 va M30 mustaqil tekshiruvi
    # -------------------------------------------------
    m15_trend = analyze_single_timeframe(m15, "M15")
    m30_trend = analyze_single_timeframe(m30, "M30")

    local_direction = m15_trend["trend"]

    counter_conditions = (
        local_direction in ("BULLISH", "BEARISH")
        and m30_trend["trend"] == local_direction
        and local_direction != overall
        and (m15_trend["adx"] or 0) >= COUNTER_MINIMUM_ADX
        and (m30_trend["adx"] or 0) >= COUNTER_MINIMUM_ADX
    )

    if counter_conditions:
        local_strength = min(
            m15_trend["strength"],
            m30_trend["strength"],
        )

        counter_result, counter_reasons = _build_trade_result(
            symbol=symbol,
            direction=local_direction,
            candles=m15,
            m30_indicators=m30_indicators,
            trend_label=f"COUNTER-TREND vs {overall}",
            strength=local_strength,
            timeframe_label="M15-CT",
        )

        if counter_result:
            counter_result.signal.reason = (
                f"COUNTER-TREND SIGNAL | "
                f"Katta trend: {overall} | "
                + counter_result.signal.reason
            )
            results.append(counter_result)

    return results
