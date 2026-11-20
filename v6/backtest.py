
import csv
import logging
import os
from bisect import bisect_right
from datetime import datetime, timezone

from .config import SYMBOLS, TIMEFRAMES
from .data_adapter import get_candles, TIMEFRAME_SETTINGS
from .signal_engine import analyze_symbol


HISTORY_LIMIT = 1000
MIN_HISTORY = 200
MAX_HOLD_BARS = 100
MAX_ENTRY_DRIFT_R = 0.25

OUTPUT_DIR = "backtest_results"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "v6_trades.csv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("V6_BACKTEST")


def candle_close_times(candles, timeframe):
    duration = TIMEFRAME_SETTINGS[timeframe]["duration_ms"]
    return [c.timestamp + duration for c in candles]


def get_closed_history(histories, close_times, signal_close_time):
    result = {}

    for timeframe in TIMEFRAMES:
        count = bisect_right(
            close_times[timeframe],
            signal_close_time,
        )
        result[timeframe] = histories[timeframe][:count]

    return result


def build_execution_levels(signal, actual_entry):
    """
    Signal shamidagi risk masofasini saqlab,
    keyingi sham ochilishiga mos SL va TP hisoblaydi.
    """

    if signal.entry is None or signal.stop_loss is None:
        return None

    reference_entry = float(signal.entry)
    original_sl = float(signal.stop_loss)

    risk_distance = abs(reference_entry - original_sl)

    if risk_distance <= 0:
        return None

    entry_drift = abs(actual_entry - reference_entry)
    drift_r = entry_drift / risk_distance

    # Narx signal chiqqan joydan juda uzoqlashgan bo'lsa, kirmaymiz.
    if drift_r > MAX_ENTRY_DRIFT_R:
        return None

    rr = signal.risk_reward or 1.5
    rr = float(rr)

    if rr < 1.0:
        return None

    if signal.side == "BUY":
        stop_loss = actual_entry - risk_distance
        take_profit = actual_entry + risk_distance * rr

    elif signal.side == "SELL":
        stop_loss = actual_entry + risk_distance
        take_profit = actual_entry - risk_distance * rr

    else:
        return None

    return {
        "reference_entry": reference_entry,
        "entry": actual_entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_distance": risk_distance,
        "risk_reward": rr,
        "entry_drift_r": drift_r,
    }


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

    for index in range(start_index, last_index + 1):
        candle = candles[index]

        if side == "BUY":
            hit_sl = candle.low <= stop_loss
            hit_tp = candle.high >= take_profit
        else:
            hit_sl = candle.high >= stop_loss
            hit_tp = candle.low <= take_profit

        # Ikkalasi bir shamda tegsa, konservativ hisobda SL olinadi.
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


def run_symbol_backtest(symbol, histories):
    m15 = histories["M15"]

    if len(m15) < MIN_HISTORY + 2:
        logger.warning("%s: M15 tarixi yetarli emas", symbol)
        return []

    close_times = {
        timeframe: candle_close_times(
            histories[timeframe],
            timeframe,
        )
        for timeframe in TIMEFRAMES
    }

    trades = []
    next_free_index = MIN_HISTORY - 1

    for index in range(MIN_HISTORY - 1, len(m15) - 1):
        if index < next_free_index:
            continue

        signal_candle = m15[index]
        signal_close_time = close_times["M15"][index]

        closed_history = get_closed_history(
            histories,
            close_times,
            signal_close_time,
        )

        if any(
            len(closed_history[tf]) < MIN_HISTORY
            for tf in TIMEFRAMES
        ):
            continue

        try:
            result = analyze_symbol(
                symbol=symbol,
                candles_by_timeframe=closed_history,
            )
        except Exception:
            logger.exception(
                "%s: %s-index tahlil xatosi",
                symbol,
                index,
            )
            continue

        signal = result.signal

        if signal.side not in ("BUY", "SELL"):
            continue

        if signal.stop_loss is None or signal.entry is None:
            continue

        # Signal yopilgandan keyingi sham ochilishida kirish.
        entry_index = index + 1
        entry_candle = m15[entry_index]
        actual_entry = float(entry_candle.open)

        levels = build_execution_levels(
            signal=signal,
            actual_entry=actual_entry,
        )

        if levels is None:
            continue

        exit_result = calculate_exit(
            candles=m15,
            start_index=entry_index,
            side=signal.side,
            stop_loss=levels["stop_loss"],
            take_profit=levels["take_profit"],
        )

        r_value = calculate_r(
            entry=levels["entry"],
            exit_price=exit_result["exit_price"],
            stop_loss=levels["stop_loss"],
            side=signal.side,
        )

        if r_value is None:
            continue

        exit_index = exit_result["exit_index"]

        trades.append({
            "symbol": symbol,
            "timeframe": "M15",
            "side": signal.side,
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
            "entry_drift_r": round(levels["entry_drift_r"], 4),
            "stop_loss": levels["stop_loss"],
            "take_profit": levels["take_profit"],
            "exit_price": exit_result["exit_price"],
            "risk_reward": levels["risk_reward"],
            "outcome": exit_result["outcome"],
            "r_multiple": round(r_value, 4),
            "technical_score": signal.confidence,
        })

        next_free_index = exit_index + 1

    return trades


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

    with open(
        OUTPUT_FILE,
        "w",
        newline="",
        encoding="utf-8",
    ) as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(trades)


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

        win_rate = wins / len(items) * 100

        print(
            f"{symbol:<10} {len(items):>5} "
            f"{win_rate:>8.2f}% {wins:>5} "
            f"{losses:>5} {timeouts:>5} "
            f"{sum(values):>10.2f} "
            f"{max_drawdown(values):>9.2f}"
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
    print("Natija foyda kafolati emas.")
    print("CSV:", OUTPUT_FILE)
    print("=" * 76)


def main():
    logger.info("V6 BACKTEST BOSHLANDI")

    all_trades = []

    for symbol in SYMBOLS:
        logger.info("[SYMBOL] %s", symbol)

        histories = {}

        try:
            for timeframe in TIMEFRAMES:
                candles = get_candles(
                    symbol=symbol,
                    timeframe=timeframe,
                    limit=HISTORY_LIMIT,
                )

                histories[timeframe] = candles

                logger.info(
                    "[DATA OK] %s %s: %s sham",
                    symbol,
                    timeframe,
                    len(candles),
                )

            trades = run_symbol_backtest(
                symbol,
                histories,
            )

            all_trades.extend(trades)

            logger.info(
                "[BACKTEST OK] %s: %s savdo",
                symbol,
                len(trades),
            )

        except Exception:
            logger.exception("[BACKTEST ERROR] %s", symbol)

    save_trades(all_trades)
    print_report(all_trades)

    logger.info("V6 BACKTEST YAKUNLANDI")


if __name__ == "__main__":
    main()
