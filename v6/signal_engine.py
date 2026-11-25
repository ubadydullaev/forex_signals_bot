
from typing import Optional

from .config import TIMEFRAMES
from .models import Signal, AnalysisResult
from .indicators import calculate_indicators
from .market_structure import analyze_market_structure
from .mtf_analysis import analyze_mtf
from .validators import validate_market_data, validate_trade


# ==========================================
# V6 SIGNAL ENGINE SETTINGS
# ==========================================

MINIMUM_CANDLES = 200
MINIMUM_ALIGNMENT = 80.0
MINIMUM_ADX = 15.0

SHORT_TERM_MINIMUM_ADX = 18.0
SHORT_TERM_CONFLICT_ADX = 22.0
SHORT_TERM_COUNTER_ADX = 22.0

SHORT_TERM_MINIMUM_CONFIRMATIONS = 2
SHORT_TERM_CONFLICT_CONFIRMATIONS = 3
SHORT_TERM_COUNTER_CONFIRMATIONS = 4

MINIMUM_RR = 1.5
STOP_ATR_MULTIPLIER = 1.5

RECENT_STRUCTURE_CANDLES = 12
RECENT_LIQUIDITY_CANDLES = 3

MACD_MOMENTUM_BARS = 4
MACD_MOMENTUM_CHANGE_THRESHOLD = 0.12
MINIMUM_ENTRY_CONFIRMATIONS = 3


# ==========================================
# ANALYSIS BATCH
# ==========================================

class AnalysisBatch:
    def __init__(self, results):
        self.results = tuple(results)
        if not self.results:
            raise ValueError("AnalysisBatch bo'sh bo'lishi mumkin")

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


# ==========================================
# COMMON HELPERS
# ==========================================

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


def _hold_result(
    symbol,
    reason,
    trend="UNKNOWN",
    strength=0.0,
    reasons=None,
):
    diagnostic_reasons = (
        tuple(reasons) if reasons is not None else (reason,)
    )
    complete_reason = "; ".join(diagnostic_reasons) or reason

    return AnalysisResult(
        symbol=symbol,
        timeframe="M15",
        signal=Signal(
            symbol=symbol,
            timeframe="M15",
            side="HOLD",
            reason=complete_reason,
        ),
        trend=trend,
        strength=strength,
        reasons=diagnostic_reasons,
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


# ==========================================
# MACD MOMENTUM ANALYSIS
# ==========================================

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
        return (
            "OPPOSING_UNCLEAR",
            "MACD qarama-qarshi, momentum uchun shamlar yetarli emas",
        )

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


# ==========================================
# MAIN TIMEFRAME VALIDATION
# ==========================================

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

    supertrend = indicators.get("supertrend", {})
    st = _last_valid(supertrend.get("direction"))

    if st == expected:
        confirmations.append("M15 Supertrend tasdiqladi")
    else:
        blockers.append("M15 Supertrend yo'nalishga qarshi")

    rsi = _last_valid(indicators.get("rsi_14"))

    if rsi is None:
        blockers.append("RSI ma'lumotlari mavjud emas")
    elif direction == "BULLISH" and 48 < rsi < 78:
        confirmations.append(f"RSI BUY: {rsi:.2f}")
    elif direction == "BEARISH" and 22 < rsi < 52:
        confirmations.append(f"RSI SELL: {rsi:.2f}")
    else:
        blockers.append(f"RSI mos emas: {rsi:.2f}")

    adx_data = indicators.get("adx", {})
    adx = _last_valid(adx_data.get("adx"))

    if adx is None or adx < MINIMUM_ADX:
        blockers.append(f"ADX yetarli emas: {adx}")
    else:
        confirmations.append(f"ADX: {adx:.2f}")

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

    structure_trend = structure.get("trend", "UNKNOWN")

    if structure_trend == direction:
        confirmations.append("Market structure tasdiqladi")
    else:
        recent_timestamps = {
            candle.timestamp
            for candle in candles[-RECENT_STRUCTURE_CANDLES:]
        }

        recent_events = [
            event for event in structure.get("events", [])
            if event.timestamp in recent_timestamps
            and event.direction == direction
        ]

        if recent_events:
            confirmations.append("Yaqinda BOS/CHOCH tasdiqlandi")
        else:
            blockers.append("Mos BOS/CHOCH topilmadi")

    if m30_indicators is not None:
        m30_ok, m30_reasons = _check_m30_momentum(
            direction,
            m30_indicators,
        )

        if not m30_ok:
            blockers.extend(m30_reasons)
        else:
            observations.extend(m30_reasons)

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


# ==========================================
# MAIN TRADE BUILDER
# ==========================================

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

    adx = _last_valid(indicators.get("adx", {}).get("adx")) or 0.0

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


# ==========================================
# EMA DIRECTION
# ==========================================

def _ema_direction(indicators):
    ema20 = _last_valid(indicators.get("ema_20"))
    ema50 = _last_valid(indicators.get("ema_50"))

    if ema20 is None or ema50 is None:
        return "UNKNOWN"

    if ema20 > ema50:
        return "BULLISH"

    if ema20 < ema50:
        return "BEARISH"

    return "UNKNOWN"


# ==========================================
# SHORT-TERM SIGNAL ENGINE
# ==========================================

def _build_short_term_result(
    symbol,
    direction,
    m15,
    m30,
    overall,
    ema_conflict=False,
    countertrend=False,
    unclear_context=False,
):
    m15_indicators = calculate_indicators(m15)
    m30_indicators = calculate_indicators(m30)

    m15_adx = _last_valid(m15_indicators.get("adx", {}).get("adx"))
    m30_adx = _last_valid(m30_indicators.get("adx", {}).get("adx"))

    diagnostics = []
    blockers = []
    confirmations = []
    observations = []

    # --------------------------------------
    # 1. ADX FILTER
    # --------------------------------------

    if m15_adx is None or m15_adx < SHORT_TERM_MINIMUM_ADX:
        return None, [f"M15 ADX past: {m15_adx}"]

    if m30_adx is None or m30_adx < SHORT_TERM_MINIMUM_ADX:
        return None, [f"M30 ADX past: {m30_adx}"]

    if ema_conflict:
        if m15_adx < SHORT_TERM_CONFLICT_ADX:
            return None, [
                f"EMA qarama-qarshi: M15 ADX {m15_adx:.2f}, "
                f"minimum {SHORT_TERM_CONFLICT_ADX:.0f}"
            ]

    if countertrend:
        if (
            m15_adx < SHORT_TERM_COUNTER_ADX
            or m30_adx < SHORT_TERM_COUNTER_ADX
        ):
            return None, [
                "COUNTER-TREND bloklandi: "
                f"M15 ADX={m15_adx:.2f}, M30 ADX={m30_adx:.2f}; "
                f"ikkalasi kamida {SHORT_TERM_COUNTER_ADX:.0f} bo'lishi kerak"
            ]

    # --------------------------------------
    # 2. M15 SUPERTREND
    # --------------------------------------

    expected = 1 if direction == "BULLISH" else -1

    m15_st = _last_valid(
        m15_indicators.get("supertrend", {}).get("direction")
    )

    st_confirmed = m15_st == expected

    if st_confirmed:
        confirmations.append("M15 Supertrend tasdiqladi")
    else:
        observations.append("M15 Supertrend yo'nalishni tasdiqlamadi")

    # --------------------------------------
    # 3. RSI
    # --------------------------------------

    rsi = _last_valid(m15_indicators.get("rsi_14"))

    if rsi is None:
        blockers.append("M15 RSI mavjud emas")
    elif direction == "BULLISH" and 45 <= rsi <= 80:
        confirmations.append(f"M15 RSI BUY momentum: {rsi:.2f}")
    elif direction == "BEARISH" and 20 <= rsi <= 55:
        confirmations.append(f"M15 RSI SELL momentum: {rsi:.2f}")
    else:
        blockers.append(f"M15 RSI momentum mos emas: {rsi:.2f}")

    # --------------------------------------
    # 4. M15 MACD
    # --------------------------------------

    m15_macd_state, m15_macd_message = _analyze_macd_momentum(
        m15_indicators.get("macd"),
        direction,
    )

    if m15_macd_state == "OPPOSING_STRENGTHENING":
        blockers.append(f"M15 {m15_macd_message}")

    elif m15_macd_state == "ALIGNED":
        confirmations.append("M15 MACD yo'nalishni tasdiqladi")

    elif m15_macd_state == "OPPOSING_WEAKENING":
        confirmations.append(m15_macd_message)

    else:
        observations.append(f"M15 {m15_macd_message}")

    # Qarama-qarshi katta trendga qarshi savdoda
    # M15 MACD albatta signal yo'nalishida bo'lishi shart.
    if countertrend and m15_macd_state != "ALIGNED":
        blockers.append(
            "COUNTER-TREND bloklandi: M15 MACD to'liq tasdiqlamadi"
        )

    # --------------------------------------
    # 5. M30 MACD
    # --------------------------------------

    m30_macd_state, m30_macd_message = _analyze_macd_momentum(
        m30_indicators.get("macd"),
        direction,
    )

    if m30_macd_state == "OPPOSING_STRENGTHENING":
        blockers.append(f"M30 {m30_macd_message}")

    elif m30_macd_state == "ALIGNED":
        confirmations.append("M30 MACD yo'nalishni tasdiqladi")

    else:
        observations.append(f"M30 {m30_macd_message}")

    # EMA qarama-qarshi yoki katta trendga qarshi savdoda
    # M30 MACD mustaqil tasdiq berishi shart.
    if (ema_conflict or countertrend) and m30_macd_state != "ALIGNED":
        blockers.append(
            "Kuchaytirilgan filtr: M30 MACD yo'nalishni tasdiqlamadi"
        )

    # --------------------------------------
    # 6. MARKET STRUCTURE
    # --------------------------------------

    structure = analyze_market_structure(m15)
    structure_trend = structure.get("trend", "UNKNOWN")

    structure_confirmed = False

    if structure_trend == direction:
        structure_confirmed = True
        confirmations.append("M15 market structure tasdiqladi")
    else:
        recent_timestamps = {
            candle.timestamp
            for candle in m15[-RECENT_STRUCTURE_CANDLES:]
        }

        recent_events = [
            event for event in structure.get("events", [])
            if event.timestamp in recent_timestamps
            and event.direction == direction
        ]

        if recent_events:
            structure_confirmed = True
            confirmations.append("M15 yaqinda BOS/CHOCH tasdiqladi")
        else:
            observations.append("M15 yangi BOS/CHOCH topilmadi")

    # --------------------------------------
    # 7. SPECIAL FILTERS
    # --------------------------------------

    if ema_conflict:
        observations.append(
            "M15/M30 EMA qarama-qarshi: kuchaytirilgan filtr ishladi"
        )

        if not st_confirmed:
            blockers.append(
                "EMA qarama-qarshi holatda M15 Supertrend tasdig'i shart"
            )

        if not structure_confirmed:
            blockers.append(
                "EMA qarama-qarshi holatda M15 BOS/CHOCH yoki structure shart"
            )

    if countertrend:
        observations.append(
            f"COUNTER-TREND: katta MTF yo'nalishi {overall}, "
            f"signal yo'nalishi {direction}"
        )

        if not st_confirmed:
            blockers.append(
                "COUNTER-TREND bloklandi: M15 Supertrend tasdig'i yo'q"
            )

        if not structure_confirmed:
            blockers.append(
                "COUNTER-TREND bloklandi: M15 structure tasdig'i yo'q"
            )

    if unclear_context:
        observations.append(
            "MTF yo'nalishi noaniq: qo'shimcha tasdiqlar talab qilinadi"
        )

        if not st_confirmed or not structure_confirmed:
            blockers.append(
                "MTF noaniq holatda Supertrend va structure ikkalasi shart"
            )

    if blockers:
        return None, blockers + confirmations + observations

    # --------------------------------------
    # 8. CONFIRMATION REQUIREMENTS
    # --------------------------------------

    required_confirmations = SHORT_TERM_MINIMUM_CONFIRMATIONS

    if ema_conflict or unclear_context:
        required_confirmations = max(
            required_confirmations,
            SHORT_TERM_CONFLICT_CONFIRMATIONS,
        )

    if countertrend:
        required_confirmations = max(
            required_confirmations,
            SHORT_TERM_COUNTER_CONFIRMATIONS,
        )

    if len(confirmations) < required_confirmations:
        diagnostics.append(
            f"Tasdiqlar yetarli emas: "
            f"{len(confirmations)}/{required_confirmations}"
        )

        return None, diagnostics + confirmations + observations

    # --------------------------------------
    # 9. ENTRY / SL / TP
    # --------------------------------------

    entry = m15[-1].close
    atr = _last_valid(m15_indicators.get("atr_14"))

    if atr is None or atr <= 0:
        return None, ["M15 ATR mavjud emas yoki noto'g'ri"]

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
        return None, list(validation.reasons)

    # --------------------------------------
    # 10. CONFIDENCE
    # --------------------------------------

    local_strength = min(m15_adx, m30_adx)

    confidence = min(
        85.0,
        local_strength * 0.7 + min(m15_adx, 50.0) * 0.6,
    )

    if ema_conflict:
        confidence = min(confidence, 82.0)

    if countertrend:
        confidence = min(confidence, 80.0)

    if unclear_context:
        confidence = min(confidence, 82.0)

    # --------------------------------------
    # 11. SIGNAL REASONS
    # --------------------------------------

    if countertrend:
        signal_type = "COUNTER-TREND SHORT-TERM"
    elif ema_conflict:
        signal_type = "EMA-CONFLICT SHORT-TERM"
    else:
        signal_type = "SHORT-TERM SIGNAL"

    reasons = [
        signal_type,
        f"M15 ADX: {m15_adx:.2f}",
        f"M30 ADX: {m30_adx:.2f}",
        f"EMA M15: {_ema_direction(m15_indicators)}",
        f"EMA M30: {_ema_direction(m30_indicators)}",
        f"MTF: {overall}",
        f"Tasdiqlar: {len(confirmations)}/{required_confirmations}",
    ] + confirmations + observations

    signal = Signal(
        symbol=symbol,
        timeframe="M15-ST",
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_reward=MINIMUM_RR,
        confidence=round(confidence, 2),
        reason="; ".join(reasons),
    )

    return AnalysisResult(
        symbol=symbol,
        timeframe="M15-ST",
        signal=signal,
        trend=f"SHORT-TERM | MTF: {overall}",
        strength=round(local_strength, 2),
        reasons=tuple(reasons),
    ), reasons


# ==========================================
# SHORT-TERM SIGNAL ROUTER
# ==========================================

def _try_short_term_signal(
    symbol,
    m15,
    m30,
    overall,
):
    m15_indicators = calculate_indicators(m15)
    m30_indicators = calculate_indicators(m30)

    m15_direction = _ema_direction(m15_indicators)
    m30_direction = _ema_direction(m30_indicators)

    if m15_direction not in ("BULLISH", "BEARISH"):
        return None, [
            f"M15 EMA yo'nalishi noaniq: {m15_direction}"
        ]

    if m30_direction == "UNKNOWN":
        return None, [
            "M30 EMA yo'nalishi noaniq: ishonchli tasdiq yo'q"
        ]

    ema_conflict = m30_direction != m15_direction

    countertrend = (
        overall in ("BULLISH", "BEARISH")
        and overall != m15_direction
    )

    unclear_context = overall not in ("BULLISH", "BEARISH")

    return _build_short_term_result(
        symbol=symbol,
        direction=m15_direction,
        m15=m15,
        m30=m30,
        overall=overall,
        ema_conflict=ema_conflict,
        countertrend=countertrend,
        unclear_context=unclear_context,
    )


# ==========================================
# COMPLETE SYMBOL ANALYSIS
# ==========================================

def analyze_symbol(symbol, candles_by_timeframe):
    # 1. Data validation
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

            return AnalysisBatch([
                _hold_result(symbol, reason)
            ])

    # 2. MTF analysis
    mtf = analyze_mtf(candles_by_timeframe)

    overall = mtf["overall_trend"]
    alignment = mtf["alignment_percent"]

    m15 = candles_by_timeframe["M15"]
    m30 = candles_by_timeframe["M30"]

    m30_indicators = calculate_indicators(m30)

    results = []
    main_signal_created = False

    # --------------------------------------
    # MAIN SIGNAL
    # --------------------------------------

    if overall in ("BULLISH", "BEARISH"):
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
                    reasons=combined,
                )
            )

        elif main_result is not None:
            main_result.signal.reason = (
                "MAIN SIGNAL | " + main_result.signal.reason
            )

            results.append(main_result)
            main_signal_created = True

        else:
            results.append(
                _hold_result(
                    symbol,
                    "MAIN signal tasdiqlanmadi: "
                    + "; ".join(main_reasons),
                    overall,
                    alignment,
                    reasons=main_reasons,
                )
            )

    else:
        results.append(
            _hold_result(
                symbol,
                "Katta timeframe'larda aniq yo'nalish yo'q",
                overall,
                alignment,
            )
        )

    # --------------------------------------
    # SHORT-TERM SIGNAL
    # --------------------------------------

    # MAIN signal yaratilmagan bo'lsa,
    # M15/M30 strategiyasi alohida tekshiriladi.
    if not main_signal_created:
        short_result, short_diagnostics = _try_short_term_signal(
            symbol=symbol,
            m15=m15,
            m30=m30,
            overall=overall,
        )

        if short_result is not None:
            results.append(short_result)

        elif short_diagnostics:
            hold = results[0]

            combined = list(hold.reasons) + [
                "SHORT-TERM: " + item
                for item in short_diagnostics
            ]

            hold.reasons = tuple(combined)
            hold.signal.reason = "; ".join(combined)

    return AnalysisBatch(results)
