
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


def get_closed_history(
    histories,
    close_times,
    signal_close_time,
):
    result = {}

    for timeframe in TIMEFRAMES:
        times = close_times[timeframe]
        count = bisect_right(times, signal_close_time)

        result[timeframe] = histories[timeframe][:count]

    return result


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

        # Bir shamda ikkalasi tegsa, SL birinchi deb olinadi.
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
    gross_profit = sum(value for value in r_values if value > 0)
    gross_loss = abs(sum(value for value in r_values if value < 0))

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
            len(closed_history[timeframe]) < MIN_HISTORY
            for timeframe in TIMEFRAMES
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

        if (
            signal.stop_loss is None
            or signal.take_profit is None
        ):
            continue

        # Signal yopilgandan keyingi M15 sham ochilishida kirish.
        entry_index = index + 1
        entry_candle = m15[entry_index]
        entry = float(entry_candle.open)

        stop_loss = float(signal.stop_loss)
        take_profit = float(signal.take_profit)

        if signal.side == "BUY":
            if not stop_loss < entry < take_profit:
                continue
        else:
            if not take_profit < entry < stop_loss:
                continue

        exit_result = calculate_exit(
            candles=m15,
            start_index=entry_index,
            side=signal.side,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

        r_value = calculate_r(
            entry=entry,
            exit_price=exit_result["exit_price"],
            stop_loss=stop_loss,
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
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "exit_price": exit_result["exit_price"],
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
        "entry",
        "stop_loss",
        "take_profit",
        "exit_price",
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
    print("\n" + "=" * 72)
    print("V6 TARIXIY BACKTEST HISOBOTI")
    print("=" * 72)

    if not trades:
        print("Hozircha tasdiqlangan savdolar topilmadi.")
        return

    groups = {}

    for trade in trades:
        key = trade["symbol"]
        groups.setdefault(key, []).append(trade)

    print(
        f"{'SYMBOL':<10} {'N':>5} {'WIN%':>9} "
        f"{'TP':>5} {'SL':>5} {'TIME':>5} "
        f"{'TOTAL R':>10} {'MAX DD':>9}"
    )
    print("-" * 72)

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

    print("-" * 72)
    print("JAMI SAVDOLAR:", len(trades))
    print("WIN RATE:", f"{wins / len(trades) * 100:.2f}%")
    print("JAMI R:", f"{sum(values):.2f}")
    print("O'RTACHA R:", f"{sum(values) / len(values):.3f}")
    print("MAX DRAWDOWN:", f"{max_drawdown(values):.2f}R")
    print("PROFIT FACTOR:", f"{profit_factor(values):.3f}")
    print("-" * 72)
    print("Eslatma: spread, komissiya va slippage hali hisoblanmagan.")
    print("Bu natija foyda kafolati emas.")
    print("CSV:", OUTPUT_FILE)
    print("=" * 72)


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
  
