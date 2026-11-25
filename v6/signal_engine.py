from typing import Optional

from .config import TIMEFRAMES
from .models import Signal, AnalysisResult
from .indicators import calculate_indicators
from .market_structure import analyze_market_structure
from .mtf_analysis import analyze_mtf, analyze_single_timeframe
from .validators import validate_market_data, validate_trade


MINIMUM_CANDLES = 200
MINIMUM_ALIGNMENT = 80.0
MINIMUM_ADX = 15.0
COUNTER_MINIMUM_ADX = 18.0
MINIMUM_RR = 1.5
STOP_ATR_MULTIPLIER = 1.5

RECENT_STRUCTURE_CANDLES = 12
RECENT_LIQUIDITY_CANDLES = 3

MACD_MOMENTUM_BARS = 4
MACD_MOMENTUM_CHANGE_THRESHOLD = 0.12
MINIMUM_ENTRY_CONFIRMATIONS = 3


class AnalysisBatch:
    """Bir nechta natijani saqlaydi va eski .signal interfeysini qo'llaydi."""

    def __init__(self, results):
        self.results = tuple(results)
        if not self.results:
            raise ValueError("AnalysisBatch bo'sh bo'lishi mumkin emas")

    @property
    def signal(self):
        return self.results[0].signal

    @property
    def trend(self):
        return self.results[0].trend

    @property
    def strength(self):
        return self.results[0].strength

    @property
    def reasons(self):
        return self.results[0].reasons

    @property
    def symbol(self):
        return self.results[0].symbol

    @property
    def timeframe(self):
        return self.results[0].timeframe

    def __iter__(self):
        return iter(self.results)

    def __len__(self):
        return len(self.results)

    def __getitem__(self, index):
        return self.results[index]


def _last_valid(values) -> Optional[float]:
    if not isinstance(values, (list, tuple)):
        return None

    for value in reversed(values):
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue

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
        macd_data.get("macd"),
        macd_data.get("signal"),
        macd_data.get("histogram"),
    )

    if not all(isinstance(x, (list, tuple)) for x in arrays):
        return []

    length = min(len(x) for x in arrays)
    rows = []

    for index in range(length):
        try:
            values = tuple(float(x[index]) for x in arrays)
            if all(value == value for value in values):
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
        return "NEUTRAL", "MACD neutral"

    if macd_direction == direction:
        return "ALIGNED", "MACD yo'nalishni tasdiqladi"

    if len(rows) < MACD_MOMENTUM_BARS:
        return "OPPOSING_UNCLEAR", "MACD qarama-qarshi, momentum uchun shamlar yetarli emas"

    magnitudes = [
        abs(row[2])
        for row in rows[-MACD_MOMENTUM_BARS:]
    ]

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

    return "OPPOSING_UNCLEAR", "MACD qarama-qarshi, momentum noaniq"


def _check_m30_momentum(direction, indicators):
    if not indicators:
        return True, ["M30 MACD tekshiruvi mavjud emas"]

    state, message = _analyze_macd_momentum(
        indicators.get("macd"),
        direction,
    )

    if state in (
        "OPPOSING_STRENGTHENING",
        "OPPOSING_UNCLEAR",
        "UNKNOWN",
    ):
        return False, [f"M30 MACD blokladi: {message}"]

    return True, [f"M30: {message}"]


def _check_direction(
    direction,
    indicators,
    structure,
    candles,
    m30_indicators=None,
):
    if direction not in ("BULLISH", "BEARISH"):
        return False, ["Aniq yo'nalish topilmadi"]

    confirmations = []
    blockers = []
    observations = []

    expected = 1 if direction == "BULLISH" else -1

    # 1. Supertrend
    supertrend = indicators.get("supertrend", {})
    st = _last_valid(supertrend.get("direction"))

    if st == expected:
        confirmations.append("M15 Supertrend tasdiqladi")
    else:
        blockers.append("M15 Supertrend yo'nalishga qarshi")

    # 2. RSI
    rsi = _last_valid(indicators.get("rsi_14"))

    if rsi is None:
        blockers.append("RSI ma'lumotlari mavjud emas")
    elif direction == "BULLISH" and 48 < rsi < 78:
        confirmations.append(f"RSI BUY: {rsi:.2f}")
    elif direction == "BEARISH" and 22 < rsi < 52:
        confirmations.append(f"RSI SELL: {rsi:.2f}")
    else:
        blockers.append(f"RSI mos emas: {rsi:.2f}")

    # 3. ADX
    adx_data = indicators.get("adx", {})
    adx = _last_valid(adx_data.get("adx"))

    if adx is None or adx < MINIMUM_ADX:
        blockers.append(f"ADX yetarli emas: {adx}")
    else:
        confirmations.append(f"ADX: {adx:.2f}")

    # 4. MACD
    macd_state, macd_message = _analyze_macd_momentum(
        indicators.get("macd"),
        direction,
    )

    if macd_state in (
        "UNKNOWN",
        "OPPOSING_UNCLEAR",
        "OPPOSING_STRENGTHENING",
    ):
        blockers.append(f"MACD blokladi: {macd_message}")
    else:
        observations.append(macd_message)

        if macd_state in ("ALIGNED", "OPPOSING_WEAKENING"):
            confirmations.append(macd_message)

    # 5. Market structure
    structure_trend = structure.get("trend", "UNKNOWN")

    if structure_trend == direction:
        confirmations.append("Market structure tasdiqladi")
    else:
        recent_timestamps = {
            candle.timestamp
            for candle in candles[-RECENT_STRUCTURE_CANDLES:]
        }

        events = structure.get("events", [])

        recent_events = [
            event for event in events
            if event.timestamp in recent_timestamps
            and event.direction == direction
        ]

        if recent_events:
            confirmations.append("Yaqinda BOS/CHOCH tasdiqlandi")
        else:
            blockers.append("Mos BOS/CHOCH topilmadi")

    # 6. M30 MACD momentum
    if m30_indicators is not None:
        m30_ok, m30_reasons = _check_m30_momentum(
            direction,
            m30_indicators,
        )

        if not m30_ok:
            blockers.extend(m30_reasons)
        else:
            observations.extend(m30_reasons)

    # 7. Liquidity sweep — qo'shimcha tasdiq
    recent_timestamps = {
        candle.timestamp
        for candle in candles[-RECENT_LIQUIDITY_CANDLES:]
    }

    sweeps = structure.get("liquidity_sweeps", [])

    matching_sweeps = [
        sweep for sweep in sweeps
        if sweep.timestamp in recent_timestamps
        and sweep.direction == direction
    ]

    if matching_sweeps:
        confirmations.append("Liquidity sweep tasdiqlandi")

    if len(confirmations) < MINIMUM_ENTRY_CONFIRMATIONS:
        blockers.append(
            f"Tasdiqlar yetarli emas: "
            f"{len(confirmations)}/{MINIMUM_ENTRY_CONFIRMATIONS}"
        )

    reasons = blockers + confirmations + observations

    return len(blockers) == 0, reasons


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
        m30_indicators=m30_indicators,
    )

    if not confirmed:
        return None, reasons

    entry = candles[-1].close
    atr = _last_valid(indicators.get("atr_14"))

    if atr is None or atr <= 0:
        return None, ["ATR mavjud emas yoki noto'g'ri"]

    side = "BUY" if direction == "BULLISH" else "SELL"
    distance = atr * STOP_ATR_MULTIPLIER

    if side == "BUY":
        stop_loss = entry - distance
        take_profit = entry + distance * MINIMUM_RR
    else:
        stop_loss = entry + distance
        take_profit = entry - distance * MINIMUM_RR

    validation = validate_trade(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        atr_value=atr,
        minimum_rr=MINIMUM_RR,
    )

    if not validation.valid:
        return None, validation.reasons

    adx = _last_valid(
        indicators.get("adx", {}).get("adx")
    ) or 0.0

    confidence = min(
        85.0,
        strength * 0.7 + min(adx, 50.0) * 0.6,
    )

    signal = Signal(
        symbol=symbol,
        timeframe=timeframe_label,
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_reward=MINIMUM_RR,
        confidence=round(confidence, 2),
        reason="; ".join(reasons),
    )

    result = AnalysisResult(
        symbol=symbol,
        timeframe=timeframe_label,
        signal=signal,
        trend=trend_label,
        strength=round(strength, 2),
        reasons=tuple(reasons),
    )

    return result, reasons


def analyze_symbol(symbol, candles_by_timeframe):
    # Barcha timeframe ma'lumotlarini tekshirish
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
            return AnalysisBatch([_hold_result(symbol, reason)])

    mtf = analyze_mtf(candles_by_timeframe)

    overall = mtf["overall_trend"]
    alignment = mtf["alignment_percent"]

    if overall not in ("BULLISH", "BEARISH"):
        return AnalysisBatch([
            _hold_result(
                symbol,
                "Katta timeframe'larda aniq yo'nalish yo'q",
                overall,
                alignment,
            )
        ])

    m15 = candles_by_timeframe["M15"]
    m30 = candles_by_timeframe["M30"]

    m30_indicators = calculate_indicators(m30)

    results = []

    # MAIN signal uchun MTF filtrlar
    main_blockers = []

    if alignment < MINIMUM_ALIGNMENT:
        main_blockers.append(
            f"MTF alignment past: {alignment:.2f}%"
        )

    if not mtf["higher_confirmation"]:
        main_blockers.append(
            "D1, H4 and H1 tasdig'i yetarli emas"
        )

    if not mtf["lower_confirmation"]:
        main_blockers.append(
            "M30 katta timeframe yo'nalishini tasdiqlamadi"
        )

    # MTF bloklasa ham M15 diagnostikasi ishlaydi
    main_result, main_reasons = _build_trade_result(
        symbol=symbol,
        direction=overall,
        candles=m15,
        m30_indicators=m30_indicators,
        trend_label=overall,
        strength=alignment,
        timeframe_label="M15",
    )

    if main_blockers:
        combined = main_blockers + main_reasons
        results.append(
            _hold_result(
                symbol,
                "; ".join(combined),
                overall,
                alignment,
            )
        )
    elif main_result is not None:
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

    # COUNTER-TREND: M15 va M30 mustaqil yo'nalishi
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

        if counter_result is not None:
            counter_result.signal.reason = (
                f"COUNTER-TREND SIGNAL | Katta trend: {overall} | "
                + counter_result.signal.reason
            )
            results.append(counter_result)

    return AnalysisBatch(results)
