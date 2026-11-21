
import csv
import logging
import math
import os
from bisect import bisect_right
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .config import SYMBOLS, TIMEFRAMES
from .data_adapter import get_candles, TIMEFRAME_SETTINGS
from .indicators import calculate_indicators
from .market_structure import SwingPoint
from .validators import validate_market_data, validate_trade


# ============================================================
# V6 HISTORICAL BACKTEST
# ============================================================

HISTORY_LIMITS = {
    "M15": 8500,
    "M30": 4250,
    "H1": 2125,
    "H4": 550,
    "D1": 300,
}

MIN_HISTORY = 200
MAX_HOLD_BARS = 100
MAX_ENTRY_DRIFT_R = 0.25

MINIMUM_ALIGNMENT = 80.0
MINIMUM_ADX = 18.0
MINIMUM_RR = 1.5
STOP_ATR_MULTIPLIER = 1.5
RECENT_STRUCTURE_CANDLES = 3

TIMEFRAME_WEIGHTS = {
    "D1": 3.0,
    "H4": 3.0,
    "H1": 2.0,
    "M30": 1.0,
    "M15": 0.5,
}
ALIGNMENT_TIMEFRAMES = ("D1", "H4", "H1", "M30")
OUTPUT_DIR = "backtest_results"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "v6_trades.csv")
DIAGNOSTICS_FILE = os.path.join(OUTPUT_DIR, "v6_diagnostics.csv")
HOLD_REASONS_FILE = os.path.join(OUTPUT_DIR, "v6_hold_reasons.csv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("V6_BACKTEST")


# ============================================================
# DIAGNOSTICS
# ============================================================

def create_diagnostics():
    return {
        "analysis_count": 0,
        "hold_count": 0,
        "buy_signals": 0,
        "sell_signals": 0,
        "accepted_entries": 0,
        "rejected_drift": 0,
        "rejected_levels": 0,
        "rejected_rr": 0,
        "rejected_invalid": 0,
        "hold_reasons": Counter(),
    }


def record_hold(diagnostics, reason):
    diagnostics["hold_count"] += 1
    reason = str(reason or "HOLD sababi ko'rsatilmagan").strip()
    diagnostics["hold_reasons"][reason] += 1


# ============================================================
# MARKET STRUCTURE
# ============================================================

class StructureTracker:
    def __init__(self, candles, left=3, right=3):
        self.candles = candles
        self.left = left
        self.right = right

        self.highs = []
        self.lows = []

        self.active_event_highs = []
        self.active_event_lows = []
        self.active_sweep_highs = []
        self.active_sweep_lows = []

        self.broken_highs = set()
        self.broken_lows = set()
        self.consumed_highs = set()
        self.consumed_lows = set()

        self.current_structure = "UNKNOWN"

        self.event_directions = [[] for _ in candles]
        self.sweep_directions = [[] for _ in candles]

        self._event_high_pointer = 0
        self._event_low_pointer = 0
        self._sweep_high_pointer = 0
        self._sweep_low_pointer = 0

        self.trends = ["UNKNOWN" for _ in candles]
        self._build()

    def _is_pivot(self, index, kind):
        if index < self.left or index + self.right >= len(self.candles):
            return False

        candle = self.candles[index]

        if kind == "HIGH":
            price = candle.high
            return all(
                price > self.candles[j].high
                for j in (
                    list(range(index - self.left, index))
                    + list(range(index + 1, index + self.right + 1))
                )
            )

        price = candle.low
        return all(
            price < self.candles[j].low
            for j in (
                list(range(index - self.left, index))
                + list(range(index + 1, index + self.right + 1))
            )
        )

    def _add_confirmed_pivot(self, index):
        if index < self.left:
            return

        candle = self.candles[index]

        if self._is_pivot(index, "HIGH"):
            self.highs.append(SwingPoint(
                index=index,
                timestamp=candle.timestamp,
                price=candle.high,
                kind="HIGH",
            ))

        if self._is_pivot(index, "LOW"):
            self.lows.append(SwingPoint(
                index=index,
                timestamp=candle.timestamp,
                price=candle.low,
                kind="LOW",
            ))

    def _activate_levels(self, candle_index):
        limit = candle_index - self.right

        while (
            self._event_high_pointer < len(self.highs)
            and self.highs[self._event_high_pointer].index < limit
        ):
            self.active_event_highs.append(
                self.highs[self._event_high_pointer]
            )
            self._event_high_pointer += 1

        while (
            self._event_low_pointer < len(self.lows)
            and self.lows[self._event_low_pointer].index < limit
        ):
            self.active_event_lows.append(
                self.lows[self._event_low_pointer]
            )
            self._event_low_pointer += 1

        while (
            self._sweep_high_pointer < len(self.highs)
            and self.highs[self._sweep_high_pointer].index < limit
        ):
            self.active_sweep_highs.append(
                self.highs[self._sweep_high_pointer]
            )
            self._sweep_high_pointer += 1

        while (
            self._sweep_low_pointer < len(self.lows)
            and self.lows[self._sweep_low_pointer].index < limit
        ):
            self.active_sweep_lows.append(
                self.lows[self._sweep_low_pointer]
            )
            self._sweep_low_pointer += 1

    def _process_events(self, index):
        if index < 1:
            return

        candle = self.candles[index]
        previous_close = self.candles[index - 1].close

        available_highs = [
            p for p in self.active_event_highs
            if p.index not in self.broken_highs
        ]

        crossed_highs = [
            p for p in available_highs
            if previous_close <= p.price < candle.close
        ]

        for point in available_highs:
            if previous_close > point.price and candle.close > point.price:
                self.broken_highs.add(point.index)

        if crossed_highs:
            self.event_directions[index].append("BULLISH")
            self.current_structure = "BULLISH"
            self.broken_highs.update(p.index for p in crossed_highs)

        available_lows = [
            p for p in self.active_event_lows
            if p.index not in self.broken_lows
        ]

        crossed_lows = [
            p for p in available_lows
            if previous_close >= p.price > candle.close
        ]

        for point in available_lows:
            if previous_close < point.price and candle.close < point.price:
                self.broken_lows.add(point.index)

        if crossed_lows:
            self.event_directions[index].append("BEARISH")
            self.current_structure = "BEARISH"
            self.broken_lows.update(p.index for p in crossed_lows)

    def _process_sweeps(self, index):
        candle = self.candles[index]

        available_highs = [
            p for p in self.active_sweep_highs
            if p.index not in self.consumed_highs
        ]

        swept_highs = [
            p for p in available_highs
            if candle.high > p.price and candle.close < p.price
        ]

        if swept_highs:
            self.sweep_directions[index].append("BEARISH")
            self.consumed_highs.update(p.index for p in swept_highs)

        for point in available_highs:
            if candle.close > point.price:
                self.consumed_highs.add(point.index)

        available_lows = [
            p for p in self.active_sweep_lows
            if p.index not in self.consumed_lows
        ]

        swept_lows = [
            p for p in available_lows
            if candle.low < p.price and candle.close > p.price
        ]

        if swept_lows:
            self.sweep_directions[index].append("BULLISH")
            self.consumed_lows.update(p.index for p in swept_lows)

        for point in available_lows:
            if candle.close < point.price:
                self.consumed_lows.add(point.index)

    def _update_trend(self, index):
        highs = [p.price for p in self.highs]
        lows = [p.price for p in self.lows]

        if len(highs) < 2 or len(lows) < 2:
            self.trends[index] = "UNKNOWN"
            return

        higher_high = highs[-1] > highs[-2]
        higher_low = lows[-1] > lows[-2]
        lower_high = highs[-1] < highs[-2]
        lower_low = lows[-1] < lows[-2]

        if higher_high and higher_low:
            self.trends[index] = "BULLISH"
        elif lower_high and lower_low:
            self.trends[index] = "BEARISH"
        else:
            self.trends[index] = "RANGING"

    def _build(self):
        for index in range(len(self.candles)):
            pivot_index = index - self.right

            if pivot_index >= self.left:
                self._add_confirmed_pivot(pivot_index)

            self._activate_levels(index)
            self._process_events(index)
            self._process_sweeps(index)
            self._update_trend(index)

    def recent_event(self, index, direction):
        start = max(0, index - RECENT_STRUCTURE_CANDLES + 1)
        return any(
            direction in self.event_directions[i]
            for i in range(start, index + 1)
        )

    def recent_sweep(self, index, direction):
        start = max(0, index - RECENT_STRUCTURE_CANDLES + 1)
        return any(
            direction in self.sweep_directions[i]
            for i in range(start, index + 1)
        )


# ============================================================
# INDICATOR CACHE
# ============================================================

class TimeframeCache:
    def __init__(self, candles):
        self.candles = candles
        self.indicators = calculate_indicators(candles)
        self.structure = StructureTracker(candles)

    def value(self, name, index):
        return self.indicators[name][index]

    def nested_value(self, group, name, index):
        return self.indicators[group][name][index]

    def structure_trend(self, index):
        return self.structure.trends[index]


# ============================================================
# TIMEFRAME ANALYSIS
# ============================================================

def analyze_cached_mtf(caches, indices):
    results = {
        tf: analyze_cached_timeframe(caches[tf], indices[tf], tf)
        for tf in TIMEFRAMES
    }

    bullish_score = 0.0
    bearish_score = 0.0

    total_weight = sum(
        TIMEFRAME_WEIGHTS[timeframe]
        for timeframe in ALIGNMENT_TIMEFRAMES
    )

    for timeframe in ALIGNMENT_TIMEFRAMES:
        result = results[timeframe]
        weight = TIMEFRAME_WEIGHTS[timeframe]

        if result["trend"] == "BULLISH":
            bullish_score += weight
        elif result["trend"] == "BEARISH":
            bearish_score += weight

    if bullish_score == 0 and bearish_score == 0:
        overall_trend = "UNKNOWN"
        alignment = 0.0

    elif bullish_score > bearish_score:
        overall_trend = "BULLISH"
        alignment = bullish_score / total_weight * 100.0

    elif bearish_score > bullish_score:
        overall_trend = "BEARISH"
        alignment = bearish_score / total_weight * 100.0

    else:
        overall_trend = "RANGING"
        alignment = max(
            bullish_score,
            bearish_score,
        ) / total_weight * 100.0

    higher_confirmation = (
        overall_trend in ("BULLISH", "BEARISH")
        and all(
            results[tf]["trend"] == overall_trend
            for tf in ("D1", "H4", "H1")
        )
    )

    lower_confirmation = (
        overall_trend in ("BULLISH", "BEARISH")
        and results["M30"]["trend"] == overall_trend
    )

    return {
        "overall_trend": overall_trend,
        "alignment_percent": round(alignment, 2),
        "higher_confirmation": higher_confirmation,
        "lower_confirmation": lower_confirmation,
    }



# ============================================================
# TRADE VALIDATION
# ============================================================

def validate_cached_trade(
    side,
    entry,
    stop_loss,
    take_profit,
    atr_value,
):
    validation = validate_trade(
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        atr_value=atr_value,
        minimum_rr=MINIMUM_RR,
    )

    return validation.valid, "; ".join(validation.reasons)


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

def analyze_cached_signal(symbol, caches, indices):
    mtf = analyze_cached_mtf(caches, indices)

    direction = mtf["overall_trend"]
    alignment = mtf["alignment_percent"]

    if direction not in ("BULLISH", "BEARISH"):
        return None, "No clear multi-timeframe direction"

    if alignment < MINIMUM_ALIGNMENT:
        return None, f"MTF alignment too low: {alignment:.2f}%"

    if not mtf["higher_confirmation"]:
        return None, "D1, H4 and H1 do not fully agree"

    if not mtf["lower_confirmation"]:
        return None, "M30 does not confirm higher-timeframe direction"

    m15_index = indices["M15"]
    m15_cache = caches["M15"]
    indicators = m15_cache.indicators
    structure = m15_cache.structure

    expected_supertrend = 1 if direction == "BULLISH" else -1

    if indicators["supertrend"]["direction"][m15_index] != expected_supertrend:
        return None, "M15 Supertrend does not confirm direction"

    rsi_value = indicators["rsi_14"][m15_index]

    if rsi_value is None:
        return None, "RSI data unavailable"

    if direction == "BULLISH" and not 50.0 < rsi_value < 75.0:
        return None, "RSI does not confirm BUY"

    if direction == "BEARISH" and not 25.0 < rsi_value < 50.0:
        return None, "RSI does not confirm SELL"

    adx_value = indicators["adx"]["adx"][m15_index]

    if adx_value is None or adx_value < MINIMUM_ADX:
        return None, "ADX trend strength is insufficient"

    macd_line = indicators["macd"]["macd"][m15_index]
    signal_line = indicators["macd"]["signal"][m15_index]
    histogram = indicators["macd"]["histogram"][m15_index]

    if any(v is None for v in (macd_line, signal_line, histogram)):
        return None, "MACD does not confirm direction"

    if direction == "BULLISH":
        macd_confirmed = macd_line > signal_line and histogram > 0
    else:
        macd_confirmed = macd_line < signal_line and histogram < 0

    if not macd_confirmed:
        return None, "MACD does not confirm direction"

    structure_trend = structure.trends[m15_index]

    if structure_trend != direction and not structure.recent_event(
        m15_index, direction
    ):
        return None, "No recent BOS/CHOCH confirmation"

    atr_value = indicators["atr_14"][m15_index]

    if atr_value is None or atr_value <= 0:
        return None, "ATR is unavailable"

    entry = m15_cache.candles[m15_index].close
    stop_distance = atr_value * STOP_ATR_MULTIPLIER
    side = "BUY" if direction == "BULLISH" else "SELL"

    if side == "BUY":
        stop_loss = entry - stop_distance
        take_profit = entry + stop_distance * MINIMUM_RR
    else:
        stop_loss = entry + stop_distance
        take_profit = entry - stop_distance * MINIMUM_RR

    levels = (entry, stop_loss, take_profit, atr_value)

    if not all(math.isfinite(v) and v > 0 for v in levels):
        return None, "Invalid trade levels"

    valid, reason = validate_cached_trade(
        side, entry, stop_loss, take_profit, atr_value
    )

    if not valid:
        return None, reason or "Trade validation failed"

    confidence = min(
        95.0,
        alignment * 0.7 + min(adx_value, 50.0) * 0.6,
    )

    return {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_reward": MINIMUM_RR,
        "atr_value": atr_value,
        "confidence": round(confidence, 2),
    }, None


# ============================================================
# CANDLE TIME MANAGEMENT
# ============================================================

def candle_close_times(candles, timeframe):
    duration = TIMEFRAME_SETTINGS[timeframe]["duration_ms"]
    return [c.timestamp + duration for c in candles]


# ============================================================
# EXECUTION LEVELS
# ============================================================

def build_execution_levels(signal, actual_entry):
    try:
        reference_entry = float(signal["entry"])
        original_sl = float(signal["stop_loss"])
        actual_entry = float(actual_entry)
        rr = float(signal["risk_reward"])
        atr_value = float(signal["atr_value"])
        side = str(signal["side"]).upper()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "invalid_levels"

    values = (reference_entry, original_sl, actual_entry, rr, atr_value)

    if not all(math.isfinite(v) and v > 0 for v in values):
        return None, "invalid_levels"

    risk_distance = abs(reference_entry - original_sl)

    if risk_distance <= 0:
        return None, "invalid_risk"

    if rr < MINIMUM_RR:
        return None, "invalid_rr"

    entry_drift = abs(actual_entry - reference_entry)
    drift_r = entry_drift / risk_distance

    if drift_r > MAX_ENTRY_DRIFT_R:
        return None, "entry_drift"

    if side == "BUY":
        stop_loss = actual_entry - risk_distance
        take_profit = actual_entry + risk_distance * rr
    elif side == "SELL":
        stop_loss = actual_entry + risk_distance
        take_profit = actual_entry - risk_distance * rr
    else:
        return None, "invalid_side"

    if stop_loss <= 0 or take_profit <= 0:
        return None, "invalid_levels"

    # Muhim: qayta tekshiruv aynan real kirish narxida bajariladi.
    valid, reason = validate_cached_trade(
        side=side,
        entry=actual_entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        atr_value=atr_value,
    )

    if not valid:
        return None, "trade_validation: " + (
            reason or "Execution trade validation failed"
        )

    return {
        "reference_entry": reference_entry,
        "entry": actual_entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_distance": risk_distance,
        "risk_reward": rr,
        "entry_drift_r": drift_r,
        "atr_value": atr_value,
    }, None


# ============================================================
# EXIT CALCULATION
# ============================================================

def calculate_exit(candles, start_index, side, stop_loss, take_profit):
    last_index = min(
        len(candles) - 1,
        start_index + MAX_HOLD_BARS - 1,
    )

    for index in range(start_index, last_index + 1):
        candle = candles[index]

        if side == "BUY":
            hit_sl = candle.low <= stop_loss
            hit_tp = candle.high >= take_profit
        else:
            hit_sl = candle.high >= stop_loss
            hit_tp = candle.low <= take_profit

        # Bir shamda SL va TP ikkalasi tegsa, konservativ ravishda SL.
        if hit_sl:
            return {
                "outcome": "SL",
                "exit_price": stop_loss,
                "exit_index": index,
            }

        if hit_tp:
            return {
                "outcome": "TP",
                "exit_price": take_profit,
                "exit_index": index,
            }

    return {
        "outcome": "TIMEOUT",
        "exit_price": candles[last_index].close,
        "exit_index": last_index,
    }


def calculate_r(entry, exit_price, stop_loss, side):
    if side == "BUY":
        risk = entry - stop_loss
        reward = exit_price - entry
    else:
        risk = stop_loss - entry
        reward = entry - exit_price

    if risk <= 0:
        return None

    return reward / risk


# ============================================================
# PERFORMANCE METRICS
# ============================================================

def max_drawdown(r_values):
    equity = 0.0
    peak = 0.0
    worst = 0.0

    for value in r_values:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)

    return worst


def profit_factor(r_values):
    gross_profit = sum(v for v in r_values if v > 0)
    gross_loss = abs(sum(v for v in r_values if v < 0))

    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0

    return gross_profit / gross_loss


# ============================================================
# SINGLE SYMBOL BACKTEST
# ============================================================

def run_symbol_backtest(symbol, histories):
    m15 = histories["M15"]
    diagnostics = create_diagnostics()

    if len(m15) < MIN_HISTORY + 2:
        return [], diagnostics

    for timeframe in TIMEFRAMES:
        validation = validate_market_data(
            histories[timeframe],
            minimum_candles=MIN_HISTORY,
        )

        if not validation.valid:
            logger.error(
                "%s %s data validation failed: %s",
                symbol,
                timeframe,
                "; ".join(validation.reasons[:5]),
            )
            return [], diagnostics

    logger.info("%s: indicators and structure preparing", symbol)

    caches = {
        tf: TimeframeCache(histories[tf])
        for tf in TIMEFRAMES
    }

    close_times = {
        tf: candle_close_times(histories[tf], tf)
        for tf in TIMEFRAMES
    }

    trades = []
    next_free_index = MIN_HISTORY - 1

    for index in range(MIN_HISTORY - 1, len(m15) - 1):
        if index < next_free_index:
            continue

        signal_close_time = close_times["M15"][index]
        indices = {}
        valid_history = True

        for timeframe in TIMEFRAMES:
            count = bisect_right(
                close_times[timeframe],
                signal_close_time,
            )

            if count < MIN_HISTORY:
                valid_history = False
                break

            indices[timeframe] = count - 1

        if not valid_history:
            continue

        diagnostics["analysis_count"] += 1

        try:
            signal, hold_reason = analyze_cached_signal(
                symbol, caches, indices
            )
        except Exception:
            diagnostics["rejected_invalid"] += 1
            logger.exception("%s index %s: signal analysis error", symbol, index)
            continue

        if signal is None:
            record_hold(diagnostics, hold_reason)
            continue

        side = signal["side"]

        if side == "BUY":
            diagnostics["buy_signals"] += 1
        else:
            diagnostics["sell_signals"] += 1

        entry_index = index + 1
        entry_candle = m15[entry_index]

        levels, rejection_reason = build_execution_levels(
            signal,
            entry_candle.open,
        )

        if levels is None:
            if rejection_reason == "entry_drift":
                diagnostics["rejected_drift"] += 1
            elif rejection_reason == "invalid_rr":
                diagnostics["rejected_rr"] += 1
            elif rejection_reason in (
                "missing_levels",
                "invalid_levels",
                "invalid_risk",
            ):
                diagnostics["rejected_levels"] += 1
            else:
                diagnostics["rejected_invalid"] += 1

            record_hold(diagnostics, rejection_reason)
            continue

        diagnostics["accepted_entries"] += 1

        exit_result = calculate_exit(
            candles=m15,
            start_index=entry_index,
            side=side,
            stop_loss=levels["stop_loss"],
            take_profit=levels["take_profit"],
        )

        r_value = calculate_r(
            entry=levels["entry"],
            exit_price=exit_result["exit_price"],
            stop_loss=levels["stop_loss"],
            side=side,
        )

        if r_value is None or not math.isfinite(r_value):
            diagnostics["rejected_invalid"] += 1
            continue

        exit_index = exit_result["exit_index"]

        trades.append({
            "symbol": symbol,
            "timeframe": "M15",
            "side": side,
            "signal_time": datetime.fromtimestamp(
                m15[index].timestamp / 1000,
                tz=timezone.utc,
            ).isoformat(),
            "entry_time": datetime.fromtimestamp(
                entry_candle.timestamp / 1000,
                tz=timezone.utc,
            ).isoformat(),
            "exit_time": datetime.fromtimestamp(
                m15[exit_index].timestamp / 1000,
                tz=timezone.utc,
            ).isoformat(),
            "reference_entry": levels["reference_entry"],
            "entry": levels["entry"],
            "entry_drift_r": round(levels["entry_drift_r"], 4),
            "stop_loss": levels["stop_loss"],
            "take_profit": levels["take_profit"],
            "exit_price": exit_result["exit_price"],
            "risk_reward": levels["risk_reward"],
            "outcome": exit_result["outcome"],
            "r_multiple": round(r_value, 4),
            "technical_score": signal["confidence"],
        })

        next_free_index = exit_index + 1

    return trades, diagnostics


# ============================================================
# PARALLEL DATA LOADING
# ============================================================

def fetch_one(task):
    symbol, timeframe, limit = task
    candles = get_candles(
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
    )
    return symbol, timeframe, candles


def load_all_histories():
    tasks = [
        (symbol, timeframe, HISTORY_LIMITS[timeframe])
        for symbol in SYMBOLS
        for timeframe in TIMEFRAMES
    ]

    histories = {symbol: {} for symbol in SYMBOLS}

    logger.info("Historical data loading with 5 workers")

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fetch_one, task): task
            for task in tasks
        }

        for future in as_completed(futures):
            symbol, timeframe, _ = futures[future]

            try:
                result_symbol, result_tf, candles = future.result()
                histories[result_symbol][result_tf] = candles

                logger.info(
                    "[DATA OK] %s %s: %s candles",
                    result_symbol,
                    result_tf,
                    len(candles),
                )
            except Exception:
                logger.exception("[DATA ERROR] %s %s", symbol, timeframe)

    ready = {}

    for symbol, data in histories.items():
        if all(tf in data for tf in TIMEFRAMES):
            ready[symbol] = data
        else:
            logger.error("%s: incomplete historical data", symbol)

    return ready


# ============================================================
# SAVE REPORTS
# ============================================================

def save_trades(trades):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    fields = [
        "symbol",
        "timeframe",
        "side",
        "signal_time",
        "entry_time",
        "exit_time",
        "reference_entry",
        "entry",
        "entry_drift_r",
        "stop_loss",
        "take_profit",
        "exit_price",
        "risk_reward",
        "outcome",
        "r_multiple",
        "technical_score",
    ]

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(trades)


def save_diagnostics(all_diagnostics):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    fields = [
        "symbol",
        "analysis_count",
        "hold_count",
        "buy_signals",
        "sell_signals",
        "accepted_entries",
        "rejected_drift",
        "rejected_levels",
        "rejected_rr",
        "rejected_invalid",
    ]

    with open(DIAGNOSTICS_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()

        for symbol, data in sorted(all_diagnostics.items()):
            row = {key: data[key] for key in fields if key != "symbol"}
            row["symbol"] = symbol
            writer.writerow(row)

    with open(HOLD_REASONS_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["symbol", "reason", "count"],
        )
        writer.writeheader()

        for symbol, data in sorted(all_diagnostics.items()):
            for reason, count in data["hold_reasons"].most_common():
                writer.writerow({
                    "symbol": symbol,
                    "reason": reason,
                    "count": count,
                })


# ============================================================
# REPORT
# ============================================================

def print_report(trades):
    print("\n" + "=" * 76)
    print("V6 HISTORICAL BACKTEST")
    print("=" * 76)

    if not trades:
        print("Tasdiqlangan savdolar topilmadi.")
        return

    groups = {}

    for trade in trades:
        groups.setdefault(trade["symbol"], []).append(trade)

    print(
        f"{'SYMBOL':<10} {'N':>5} {'WIN%':>9} "
        f"{'TP':>5} {'SL':>5} {'TIME':>5} "
        f"{'TOTAL R':>10} {'MAX DD':>9}"
    )
    print("-" * 76)

    for symbol, items in sorted(groups.items()):
        values = [item["r_multiple"] for item in items]
        wins = sum(item["outcome"] == "TP" for item in items)
        losses = sum(item["outcome"] == "SL" for item in items)
        timeouts = sum(item["outcome"] == "TIMEOUT" for item in items)

        print(
            f"{symbol:<10} {len(items):>5} "
            f"{wins / len(items) * 100:>8.2f}% "
            f"{wins:>5} {losses:>5} {timeouts:>5} "
            f"{sum(values):>10.2f} {max_drawdown(values):>9.2f}"
        )

    values = [item["r_multiple"] for item in trades]
    wins = sum(item["outcome"] == "TP" for item in trades)

    print("-" * 76)
    print("JAMI SAVDOLAR:", len(trades))
    print("WIN RATE:", f"{wins / len(trades) * 100:.2f}%")
    print("JAMI R:", f"{sum(values):.2f}")
    print("O'RTACHA R:", f"{sum(values) / len(values):.3f}")
    print("MAX DRAWDOWN:", f"{max_drawdown(values):.2f}R")
    print("PROFIT FACTOR:", f"{profit_factor(values):.3f}")
    print("-" * 76)
    print("Spread, komissiya va slippage hisobga olinmagan.")
    print("Tarixiy natija foyda kafolati emas.")
    print("CSV:", OUTPUT_FILE)
    print("=" * 76)


def print_diagnostics(all_diagnostics):
    print("\n" + "=" * 76)
    print("V6 SIGNAL DIAGNOSTICS")
    print("=" * 76)

    for symbol, data in sorted(all_diagnostics.items()):
        print(f"\n[{symbol}]")

        labels = (
            ("analysis_count", "Tahlil qilingan shamlar"),
            ("hold_count", "HOLD"),
            ("buy_signals", "BUY"),
            ("sell_signals", "SELL"),
            ("accepted_entries", "Qabul qilingan kirishlar"),
            ("rejected_drift", "Narx siljishi sabab rad"),
            ("rejected_levels", "SL/TP yoki risk sabab rad"),
            ("rejected_rr", "RR sabab rad"),
            ("rejected_invalid", "Boshqa xatolar"),
        )

        for key, label in labels:
            print(f"{label}: {data[key]}")

        if data["hold_reasons"]:
            print("HOLD SABABLARI:")
            for reason, count in data["hold_reasons"].most_common(10):
                print(f"  {count:>5}x | {reason}")

    print("=" * 76)


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info("V6 BACKTEST STARTED")

    all_trades = []
    all_diagnostics = {}

    histories_by_symbol = load_all_histories()

    for symbol in SYMBOLS:
        if symbol not in histories_by_symbol:
            all_diagnostics[symbol] = create_diagnostics()
            continue

        logger.info("[BACKTEST] %s", symbol)

        try:
            trades, diagnostics = run_symbol_backtest(
                symbol,
                histories_by_symbol[symbol],
            )

            all_trades.extend(trades)
            all_diagnostics[symbol] = diagnostics

            logger.info(
                "[BACKTEST OK] %s: %s trades | BUY %s | SELL %s | HOLD %s",
                symbol,
                len(trades),
                diagnostics["buy_signals"],
                diagnostics["sell_signals"],
                diagnostics["hold_count"],
            )

        except Exception:
            logger.exception("[BACKTEST ERROR] %s", symbol)
            all_diagnostics[symbol] = create_diagnostics()

    save_trades(all_trades)
    save_diagnostics(all_diagnostics)

    print_report(all_trades)
    print_diagnostics(all_diagnostics)

    logger.info("V6 BACKTEST FINISHED")


if __name__ == "__main__":
    main()
