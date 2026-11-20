
import csv
import logging
import os
from bisect import bisect_right
from collections import Counter

from datetime import datetime, timezone

from .config import SYMBOLS, TIMEFRAMES
from .data_adapter import get_candles, TIMEFRAME_SETTINGS
from .signal_engine import analyze_symbol


# ==========================================
# V6 BACKTEST SETTINGS
# ==========================================

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

OUTPUT_DIR = "backtest_results"

OUTPUT_FILE = os.path.join(
    OUTPUT_DIR,
    "v6_trades.csv",
)

DIAGNOSTICS_FILE = os.path.join(
    OUTPUT_DIR,
    "v6_diagnostics.csv",
)

HOLD_REASONS_FILE = os.path.join(
    OUTPUT_DIR,
    "v6_hold_reasons.csv",
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger("V6_BACKTEST")


# ==========================================
# DIAGNOSTICS
# ==========================================

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

    reason = str(
        reason or "HOLD sababi ko'rsatilmagan"
    ).strip()

    if not reason:
        reason = "HOLD sababi ko'rsatilmagan"

    diagnostics["hold_reasons"][reason] += 1


# ==========================================
# CANDLE TIME MANAGEMENT
# ==========================================

def candle_close_times(candles, timeframe):
    duration = TIMEFRAME_SETTINGS[
        timeframe
    ]["duration_ms"]

    return [
        candle.timestamp + duration
        for candle in candles
    ]


def get_closed_history(
    histories,
    close_times,
    signal_close_time,
):
    result = {}

    for timeframe in TIMEFRAMES:
        count = bisect_right(
            close_times[timeframe],
            signal_close_time,
        )

        result[timeframe] = (
            histories[timeframe][:count]
        )

    return result


# ==========================================
# EXECUTION LEVELS
# ==========================================

def build_execution_levels(
    signal,
    actual_entry,
):
    """
    Signal shamidagi risk masofasini saqlaydi.
    Keyingi M15 sham ochilishida kirish narxini oladi.

    Natija:
        (levels, None) - qabul qilindi
        (None, reason) - rad etildi
    """

    if (
        signal.entry is None
        or signal.stop_loss is None
        or signal.take_profit is None
    ):
        return None, "missing_levels"

    try:
        reference_entry = float(signal.entry)
        original_sl = float(signal.stop_loss)
        actual_entry = float(actual_entry)

        rr = float(
            signal.risk_reward or 1.5
        )

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return None, "invalid_levels"

    if not all(
        map(
            lambda value: (
                value > 0
                and value != float("inf")
                and value == value
            ),
            (
                reference_entry,
                original_sl,
                actual_entry,
                rr,
            ),
        )
    ):
        return None, "invalid_levels"

    risk_distance = abs(
        reference_entry - original_sl
    )

    if risk_distance <= 0:
        return None, "invalid_risk"

    if rr < 1.0:
        return None, "invalid_rr"

    entry_drift = abs(
        actual_entry - reference_entry
    )

    drift_r = entry_drift / risk_distance

    if drift_r > MAX_ENTRY_DRIFT_R:
        return None, "entry_drift"

    if signal.side == "BUY":
        stop_loss = actual_entry - risk_distance
        take_profit = (
            actual_entry + risk_distance * rr
        )

    elif signal.side == "SELL":
        stop_loss = actual_entry + risk_distance
        take_profit = (
            actual_entry - risk_distance * rr
        )

    else:
        return None, "invalid_side"

    if stop_loss <= 0 or take_profit <= 0:
        return None, "invalid_levels"

    return {
        "reference_entry": reference_entry,
        "entry": actual_entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_distance": risk_distance,
        "risk_reward": rr,
        "entry_drift_r": drift_r,
    }, None


# ==========================================
# EXIT CALCULATION
# ==========================================

def calculate_exit(
    candles,
    start_index,
    side,
    stop_loss,
    take_profit,
):
    last_index = min(
        len(candles) - 1,
        start_index + MAX_HOLD_BARS - 1,
    )

    for index in range(
        start_index,
        last_index + 1,
    ):
        candle = candles[index]

        if side == "BUY":
            hit_sl = candle.low <= stop_loss
            hit_tp = candle.high >= take_profit

        else:
            hit_sl = candle.high >= stop_loss
            hit_tp = candle.low <= take_profit

        # Bir shamda SL va TP tegsa,
        # konservativ hisobda SL olinadi.
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


# ==========================================
# R MULTIPLE
# ==========================================

def calculate_r(
    entry,
    exit_price,
    stop_loss,
    side,
):
    if side == "BUY":
        risk = entry - stop_loss
        reward = exit_price - entry

    else:
        risk = stop_loss - entry
        reward = entry - exit_price

    if risk <= 0:
        return None

    return reward / risk


# ==========================================
# PERFORMANCE METRICS
# ==========================================

def max_drawdown(r_values):
    equity = 0.0
    peak = 0.0
    worst = 0.0

    for value in r_values:
        equity += value
        peak = max(peak, equity)

        worst = min(
            worst,
            equity - peak,
        )

    return worst


def profit_factor(r_values):
    gross_profit = sum(
        value
        for value in r_values
        if value > 0
    )

    gross_loss = abs(
        sum(
            value
            for value in r_values
            if value < 0
        )
    )

    if gross_loss == 0:
        return (
            float("inf")
            if gross_profit > 0
            else 0.0
        )

    return gross_profit / gross_loss


# ==========================================
# SINGLE SYMBOL BACKTEST
# ==========================================

def run_symbol_backtest(symbol, histories):
    m15 = histories["M15"]

    diagnostics = create_diagnostics()

    if len(m15) < MIN_HISTORY + 2:
        logger.warning(
            "%s: M15 tarixi yetarli emas",
            symbol,
        )

        return [], diagnostics

    close_times = {
        timeframe: candle_close_times(
            histories[timeframe],
            timeframe,
        )
        for timeframe in TIMEFRAMES
    }

    trades = []

    next_free_index = MIN_HISTORY - 1

    for index in range(
        MIN_HISTORY - 1,
        len(m15) - 1,
    ):
        if index < next_free_index:
            continue

        signal_candle = m15[index]

        signal_close_time = (
            close_times["M15"][index]
        )

        closed_history = get_closed_history(
            histories,
            close_times,
            signal_close_time,
        )

        if any(
            len(closed_history[timeframe]) < MIN_HISTORY
            for timeframe in TIMEFRAMES
        ):
            continue

        diagnostics["analysis_count"] += 1

        try:
            result = analyze_symbol(
                symbol=symbol,
                candles_by_timeframe=closed_history,
            )

        except Exception:
            diagnostics["rejected_invalid"] += 1

            logger.exception(
                "%s: %s-index tahlil xatosi",
                symbol,
                index,
            )

            continue

        signal = getattr(
            result,
            "signal",
            None,
        )

        if signal is None:
            diagnostics["rejected_invalid"] += 1
            continue

        side = getattr(
            signal,
            "side",
            "HOLD",
        )

        if side not in ("BUY", "SELL"):
            reasons = getattr(
                result,
                "reasons",
                (),
            )

            reason = getattr(
                signal,
                "reason",
                "",
            )

            if not reason and reasons:
                reason = "; ".join(
                    str(item)
                    for item in reasons
                )

            record_hold(
                diagnostics,
                reason,
            )

            continue

        if side == "BUY":
            diagnostics["buy_signals"] += 1

        else:
            diagnostics["sell_signals"] += 1

        # Signal yopilgandan keyingi sham ochilishi.
        entry_index = index + 1

        entry_candle = m15[entry_index]

        actual_entry = float(
            entry_candle.open
        )

        levels, rejection_reason = (
            build_execution_levels(
                signal=signal,
                actual_entry=actual_entry,
            )
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

        if r_value is None:
            diagnostics["rejected_invalid"] += 1
            continue

        exit_index = exit_result["exit_index"]

        trades.append({
            "symbol": symbol,
            "timeframe": "M15",
            "side": side,

            "signal_time": datetime.fromtimestamp(
                signal_candle.timestamp / 1000,
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
            "entry_drift_r": round(
                levels["entry_drift_r"],
                4,
            ),

            "stop_loss": levels["stop_loss"],
            "take_profit": levels["take_profit"],
            "exit_price": exit_result["exit_price"],

            "risk_reward": levels["risk_reward"],
            "outcome": exit_result["outcome"],
            "r_multiple": round(r_value, 4),

            "technical_score": signal.confidence,
        })

        next_free_index = exit_index + 1

    return trades, diagnostics


# ==========================================
# SAVE TRADE REPORT
# ==========================================

def save_trades(trades):
    os.makedirs(
        OUTPUT_DIR,
        exist_ok=True,
    )

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

    with open(
        OUTPUT_FILE,
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields,
        )

        writer.writeheader()
        writer.writerows(trades)


# ==========================================
# SAVE DIAGNOSTICS
# ==========================================

def save_diagnostics(all_diagnostics):
    os.makedirs(
        OUTPUT_DIR,
        exist_ok=True,
    )

    diagnostic_fields = [
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

    with open(
        DIAGNOSTICS_FILE,
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=diagnostic_fields,
        )

        writer.writeheader()

        for symbol, data in sorted(
            all_diagnostics.items()
        ):
            writer.writerow({
                key: data[key]
                for key in diagnostic_fields
                if key != "symbol"
            } | {
                "symbol": symbol,
            })

    with open(
        HOLD_REASONS_FILE,
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        fields = [
            "symbol",
            "reason",
            "count",
        ]

        writer = csv.DictWriter(
            file,
            fieldnames=fields,
        )

        writer.writeheader()

        for symbol, data in sorted(
            all_diagnostics.items()
        ):
            for reason, count in (
                data["hold_reasons"].most_common()
            ):
                writer.writerow({
                    "symbol": symbol,
                    "reason": reason,
                    "count": count,
                })


# ==========================================
# PRINT DIAGNOSTICS
# ==========================================

def print_diagnostics(all_diagnostics):
    print("\n" + "=" * 76)
    print("V6 SIGNAL DIAGNOSTIKASI")
    print("=" * 76)

    for symbol, data in sorted(
        all_diagnostics.items()
    ):
        print(f"\n[{symbol}]")

        print(
            "Tahlil qilingan shamlar:",
            data["analysis_count"],
        )

        print(
            "HOLD:",
            data["hold_count"],
        )

        print(
            "BUY:",
            data["buy_signals"],
        )

        print(
            "SELL:",
            data["sell_signals"],
        )

        print(
            "Qabul qilingan kirishlar:",
            data["accepted_entries"],
        )

        print(
            "Narx siljishi sabab rad:",
            data["rejected_drift"],
        )

        print(
            "SL/TP yoki risk sabab rad:",
            data["rejected_levels"],
        )

        print(
            "RR sabab rad:",
            data["rejected_rr"],
        )

        print(
            "Boshqa xatolar:",
            data["rejected_invalid"],
        )

        if data["hold_reasons"]:
            print("HOLD SABABLARI:")

            for reason, count in (
                data["hold_reasons"].most_common(10)
            ):
                print(
                    f"  {count:>5}x | {reason}"
                )

    print("=" * 76)


# ==========================================
# PERFORMANCE REPORT
# ==========================================

def print_report(trades):
    print("\n" + "=" * 76)
    print("V6 TARIXIY BACKTEST HISOBOTI")
    print("=" * 76)

    if not trades:
        print("Tasdiqlangan savdolar topilmadi.")
        print("CSV:", OUTPUT_FILE)
        return

    groups = {}

    for trade in trades:
        groups.setdefault(
            trade["symbol"],
            [],
        ).append(trade)

    print(
        f"{'SYMBOL':<10} {'N':>5} {'WIN%':>9} "
        f"{'TP':>5} {'SL':>5} {'TIME':>5} "
        f"{'TOTAL R':>10} {'MAX DD':>9}"
    )

    print("-" * 76)

    for symbol, items in sorted(groups.items()):
        values = [
            item["r_multiple"]
            for item in items
        ]

        wins = sum(
            item["outcome"] == "TP"
            for item in items
        )

        losses = sum(
            item["outcome"] == "SL"
            for item in items
        )

        timeouts = sum(
            item["outcome"] == "TIMEOUT"
            for item in items
        )

        win_rate = wins / len(items) * 100

        print(
            f"{symbol:<10} {len(items):>5} "
            f"{win_rate:>8.2f}% {wins:>5} "
            f"{losses:>5} {timeouts:>5} "
            f"{sum(values):>10.2f} "
            f"{max_drawdown(values):>9.2f}"
        )

    values = [
        item["r_multiple"]
        for item in trades
    ]

    wins = sum(
        item["outcome"] == "TP"
        for item in trades
    )

    print("-" * 76)

    print("JAMI SAVDOLAR:", len(trades))

    print(
        "WIN RATE:",
        f"{wins / len(trades) * 100:.2f}%",
    )

    print(
        "JAMI R:",
        f"{sum(values):.2f}",
    )

    print(
        "O'RTACHA R:",
        f"{sum(values) / len(values):.3f}",
    )

    print(
        "MAX DRAWDOWN:",
        f"{max_drawdown(values):.2f}R",
    )

    print(
        "PROFIT FACTOR:",
        f"{profit_factor(values):.3f}",
    )

    print("-" * 76)

    print(
        "Spread, komissiya va slippage "
        "hisobga olinmagan."
    )

    print(
        "Natija foyda kafolati emas."
    )

    print("CSV:", OUTPUT_FILE)
    print("=" * 76)


# ==========================================
# MAIN
# ==========================================

def main():
    logger.info(
        "V6 TARIXIY BACKTEST BOSHLANDI"
    )

    all_trades = []
    all_diagnostics = {}

    for symbol in SYMBOLS:
        logger.info(
            "[SYMBOL] %s",
            symbol,
        )

        histories = {}

        all_diagnostics[symbol] = (
            create_diagnostics()
        )

        try:
            for timeframe in TIMEFRAMES:
                limit = HISTORY_LIMITS[timeframe]

                candles = get_candles(
                    symbol=symbol,
                    timeframe=timeframe,
                    limit=limit,
                )

                histories[timeframe] = candles

                logger.info(
                    "[DATA OK] %s %s: %s / %s sham",
                    symbol,
                    timeframe,
                    len(candles),
                    limit,
                )

            trades, diagnostics = (
                run_symbol_backtest(
                    symbol,
                    histories,
                )
            )

            all_trades.extend(trades)

            all_diagnostics[symbol] = diagnostics

            logger.info(
                "[BACKTEST OK] %s: "
                "%s savdo | BUY %s | SELL %s | HOLD %s",
                symbol,
                len(trades),
                diagnostics["buy_signals"],
                diagnostics["sell_signals"],
                diagnostics["hold_count"],
            )

        except Exception:
            logger.exception(
                "[BACKTEST ERROR] %s",
                symbol,
            )

    save_trades(all_trades)

    save_diagnostics(
        all_diagnostics
    )

    print_report(all_trades)

    print_diagnostics(
        all_diagnostics
    )

    logger.info(
        "V6 BACKTEST YAKUNLANDI"
    )


if __name__ == "__main__":
    main()
