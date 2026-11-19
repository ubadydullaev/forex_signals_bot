
import os
import json
import math
import logging
import subprocess
from datetime import datetime, timedelta, timezone

import pandas as pd

from advanced_analysis import analyze_advanced
from btc_data import get_btc_candles


SYMBOLS = [
    "XAUUSD", "EURUSD", "GBPUSD", "USDJPY",
    "AUDUSD", "USDCAD", "USDCHF", "US500",
    "BTCUSD", "DE30", "XAGUSD", "BRENT",
]

DUKASCOPY_SYMBOLS = {
    "XAUUSD": "xauusd",
    "EURUSD": "eurusd",
    "GBPUSD": "gbpusd",
    "USDJPY": "usdjpy",
    "AUDUSD": "audusd",
    "USDCAD": "usdcad",
    "USDCHF": "usdchf",
    "US500": "usa500idxusd",
    "DE30": "deuidxeur",
    "XAGUSD": "xagusd",
    "BRENT": "brentcmdusd",
}

TIMEFRAMES = {
    "M15": ("15m", 15 * 60 * 1000),
    "M30": ("30m", 30 * 60 * 1000),
    "H1": ("1h", 60 * 60 * 1000),
    "H4": ("4h", 4 * 60 * 60 * 1000),
    "D1": ("1d", 24 * 60 * 60 * 1000),
}

MAX_TEST_BARS = 10000
MAX_HOLD_BARS = 100
MIN_HISTORY = 220

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def get_history(symbol, interval):
    if symbol == "BTCUSD":
        return get_btc_candles(interval, limit=1000)

    tf_map = {
        "15m": ("m15", 7),
        "30m": ("m30", 180),
        "1h": ("h1", 180),
        "4h": ("h4", 180),
        "1d": ("d1", 500),
    }

    dukascopy_tf, days = tf_map[interval]
    instrument = DUKASCOPY_SYMBOLS[symbol]

    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    os.makedirs("download", exist_ok=True)
    filename = (
        f"backtest_{instrument}_{dukascopy_tf}_"
        f"{now.strftime('%Y%m%d%H%M%S')}"
    )
    output = os.path.abspath(
        os.path.join("download", filename + ".json")
    )

    command = [
        "npx", "--no-install", "dukascopy-node",
        "-i", instrument,
        "-from", start,
        "-to", end,
        "-t", dukascopy_tf,
        "-f", "json",
        "-dir", os.path.abspath("download"),
        "-fn", filename,
        "--cache",
        "--retries", "3",
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=240,
    )

    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout or "Dukascopy xatosi")[-2000:]
        )

    if not os.path.exists(output):
        raise RuntimeError("Tarixiy JSON fayl topilmadi")

    with open(output, "r", encoding="utf-8") as file:
        raw = json.load(file)

    candle_ms = TIMEFRAMES[{
        "m15": "M15",
        "m30": "M30",
        "h1": "H1",
        "h4": "H4",
        "d1": "D1",
    }[dukascopy_tf]][1]

    now_ms = int(now.timestamp() * 1000)
    unique = {}

    for item in raw:
        try:
            ts = int(item["timestamp"])
            if ts + candle_ms > now_ms:
                continue

            candle = {
                "timestamp": ts,
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            }

            prices = [
                candle["open"], candle["high"],
                candle["low"], candle["close"],
            ]

            if not all(math.isfinite(x) and x > 0 for x in prices):
                continue
            if candle["high"] < max(candle["open"], candle["close"], candle["low"]):
                continue
            if candle["low"] > min(candle["open"], candle["close"], candle["high"]):
                continue

            unique[ts] = candle
        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    candles = sorted(unique.values(), key=lambda x: x["timestamp"])

    if len(candles) < MIN_HISTORY:
        raise RuntimeError(f"Tarixiy shamlar yetarli emas: {len(candles)}")

    return candles


def higher_timeframes_for(tf):
    if tf in ("M15", "M30"):
        return ["H1", "H4", "D1"]
    if tf == "H1":
        return ["H4", "D1"]
    return ["D1"]


def make_frame(candles):
    return pd.DataFrame(candles).reset_index(drop=True)


def run_one_backtest(symbol, tf, histories):
    candles = histories.get(tf, [])
    if len(candles) < MIN_HISTORY + 2:
        return []

    entry_ms = TIMEFRAMES[tf][1]
    required_htfs = higher_timeframes_for(tf)
    trades = []

    first = max(MIN_HISTORY, len(candles) - MAX_TEST_BARS - 1)
    last = len(candles) - 1

    next_free_index = first
    for i in range(first, last):
        if i < next_free_index:
            continue
        signal_candle = candles[i]
        signal_close_time = signal_candle["timestamp"] + entry_ms

        entry_history = candles[:i + 1]
        higher = {}

        for htf in required_htfs:
            htf_ms = TIMEFRAMES[htf][1]
            htf_candles = histories.get(htf, [])

            closed = [
                c for c in htf_candles
                if c["timestamp"] + htf_ms <= signal_close_time
            ]

            if len(closed) >= 60:
                higher[htf] = make_frame(closed)

        try:
            result = analyze_advanced(
                entry_df=make_frame(entry_history),
                higher_timeframes=higher,
            )
        except Exception as exc:
            logging.warning(
                "%s %s bar %s tahlil xatosi: %s",
                symbol, tf, i, exc,
            )
            continue

        direction = result.get("signal")
        stop = result.get("stop_loss")
        target = result.get("take_profit")

        if direction not in ("BUY", "SELL") or stop is None or target is None:
            continue

        # Signal yopilgandan keyingi sham ochilishida kirish.
        entry_bar = candles[i + 1]
        entry = float(entry_bar["open"])
        stop = float(stop)
        target = float(target)

        if direction == "BUY":
            risk = entry - stop
            if risk <= 0:
                continue
        else:
            risk = stop - entry
            if risk <= 0:
                continue

        outcome = "TIMEOUT"
        exit_price = None
        exit_index = None

        for j in range(i + 1, min(len(candles), i + 1 + MAX_HOLD_BARS)):
            bar = candles[j]
            high = float(bar["high"])
            low = float(bar["low"])

            if direction == "BUY":
                hit_sl = low <= stop
                hit_tp = high >= target
            else:
                hit_sl = high >= stop
                hit_tp = low <= target

            # OHLC ichida qaysi daraja avval tegilgani noma'lum.
            # Ikkalasi tegsa, konservativ ravishda SL deb olinadi.
            if hit_sl:
                outcome = "SL"
                exit_price = stop
                exit_index = j
                break

            if hit_tp:
                outcome = "TP"
                exit_price = target
                exit_index = j
                break

        if exit_price is None:
            final_index = min(
                len(candles) - 1,
                i + MAX_HOLD_BARS,
            )
            if final_index <= i:
                continue
            exit_index = final_index
            exit_price = float(candles[final_index]["close"])

        if direction == "BUY":
            r_multiple = (exit_price - entry) / risk
        else:
            r_multiple = (entry - exit_price) / risk

        trades.append({
            "symbol": symbol,
            "timeframe": tf,
            "direction": direction,
            "entry_time": entry_bar["timestamp"],
            "exit_time": candles[exit_index]["timestamp"],
            "entry": entry,
            "stop": stop,
            "target": target,
            "exit": exit_price,
            "outcome": outcome,
            "r": r_multiple,
        })
        next_free_index = exit_index
    return trades


def max_drawdown(r_values):
    equity = 0.0
    peak = 0.0
    worst = 0.0

    for value in r_values:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)

    return worst


def print_report(all_trades):
    print("\n" + "=" * 72)
    print("ADVANCED STRATEGY — TARIXIY BACKTEST HISOBOTI")
    print("=" * 72)

    if not all_trades:
        print("Signal topilmadi. Natija chiqarish uchun savdolar yetarli emas.")
        return

    groups = {}

    for trade in all_trades:
        key = (trade["symbol"], trade["timeframe"])
        groups.setdefault(key, []).append(trade)

    print(
        f"{'SYMBOL':<9} {'TF':<5} {'N':>5} "
        f"{'WIN%':>8} {'TP':>5} {'SL':>5} "
        f"{'TIME':>5} {'TOTAL R':>10} {'MAX DD':>9}"
    )
    print("-" * 72)

    for (symbol, tf), trades in sorted(groups.items()):
        wins = sum(t["outcome"] == "TP" for t in trades)
        losses = sum(t["outcome"] == "SL" for t in trades)
        timeouts = sum(t["outcome"] == "TIMEOUT" for t in trades)
        total = len(trades)
        win_rate = wins / total * 100 if total else 0
        r_values = [t["r"] for t in trades]
        total_r = sum(r_values)
        drawdown = max_drawdown(r_values)

        print(
            f"{symbol:<9} {tf:<5} {total:>5} "
            f"{win_rate:>7.1f}% {wins:>5} {losses:>5} "
            f"{timeouts:>5} {total_r:>10.2f} {drawdown:>9.2f}"
        )

    values = [t["r"] for t in all_trades]
    wins = sum(t["outcome"] == "TP" for t in all_trades)

    print("-" * 72)
    print(f"Jami savdolar: {len(all_trades)}")
    print(f"Win rate: {wins / len(all_trades) * 100:.2f}%")
    print(f"Jami natija: {sum(values):.2f}R")
    print(f"O'rtacha natija: {sum(values) / len(values):.3f}R")
    print(f"Max drawdown: {max_drawdown(values):.2f}R")

    os.makedirs("backtest_results", exist_ok=True)
    output = os.path.join("backtest_results", "advanced_trades.csv")
    pd.DataFrame(all_trades).to_csv(output, index=False)

    print(f"To'liq savdolar CSV faylida: {output}")
    print("=" * 72)


def main():
    logging.info("Advanced backtest boshlandi")
    all_trades = []

    for symbol in SYMBOLS:
        histories = {}

        for tf, (interval, _) in TIMEFRAMES.items():
            try:
                histories[tf] = get_history(symbol, interval)
                logging.info(
                    "%s %s: %s sham",
                    symbol, tf, len(histories[tf]),
                )
            except Exception as exc:
                logging.warning(
                    "%s %s tarix olinmadi: %s",
                    symbol, tf, exc,
                )

        for tf in ("M30", "H1"):
            if tf not in histories:
                continue

            try:
                trades = run_one_backtest(symbol, tf, histories)
                all_trades.extend(trades)
                logging.info(
                    "%s %s: %s ta savdo",
                    symbol, tf, len(trades),
                )
            except Exception:
                logging.exception("%s %s backtest xatosi", symbol, tf)

    print_report(all_trades)
    logging.info("Advanced backtest yakunlandi")


if __name__ == "__main__":
    main()
  
