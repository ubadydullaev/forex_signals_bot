
"""
V5.1 HISTORICAL BACKTEST
Telegram yubormaydi va bot_v4.py / bot_v5.py ni o'zgartirmaydi.
"""

import os
import csv
import math
import logging

# bot_v4 import vaqtida talab qiladigan qiymatlar.
# Backtest ularni ishlatmaydi.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "BACKTEST_ONLY")
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")
os.environ.setdefault("CLOUDFLARE_WORKER_URL", "https://example.invalid")
os.environ.setdefault("WEBHOOK_SECRET", "BACKTEST_ONLY")
os.environ.setdefault("GITHUB_TOKEN", "BACKTEST_ONLY")
os.environ.setdefault("GITHUB_REPOSITORY", "BACKTEST_ONLY")
os.environ.setdefault("GITHUB_REF_NAME", "main")

import bot_v5

core = bot_v5.core

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

DURATION_MS = {
    "M15": 15 * 60 * 1000,
    "M30": 30 * 60 * 1000,
    "H1": 60 * 60 * 1000,
    "H4": 4 * 60 * 60 * 1000,
    "D1": 24 * 60 * 60 * 1000,
}

MAX_TEST_BARS = 100
MAX_HOLDING_BARS = 100
OUTPUT_FILE = "backtest_v5_results.csv"


def candle_timestamp(candle):
    return int(candle.get("timestamp", 0))


def closed_candles(candles, timeframe, cutoff):
    duration = DURATION_MS[timeframe]

    return [
        candle for candle in candles
        if candle_timestamp(candle) + duration <= cutoff
    ]


def calculate_exit(side, entry, sl, tp, future):
    """
    Natija:
    exit_price, result_R, reason, bars_held
    """

    risk = abs(entry - sl)

    if risk <= 0:
        return None

    for held, candle in enumerate(future, start=1):

        open_price = float(candle["open"])
        high = float(candle["high"])
        low = float(candle["low"])

        if side == "BUY":

            # SL dan pastda ochilsa, yomonroq narxda yopiladi.
            if open_price <= sl:
                result = (open_price - entry) / risk
                return open_price, result, "SL_GAP", held

            hit_sl = low <= sl
            hit_tp = high >= tp

            if hit_sl and hit_tp:
                return sl, -1.0, "SL_BOTH_TOUCHED", held

            if hit_sl:
                return sl, -1.0, "SL", held

            if hit_tp:
                result = (tp - entry) / risk
                return tp, result, "TP", held

        elif side == "SELL":

            if open_price >= sl:
                result = (entry - open_price) / risk
                return open_price, result, "SL_GAP", held

            hit_sl = high >= sl
            hit_tp = low <= tp

            if hit_sl and hit_tp:
                return sl, -1.0, "SL_BOTH_TOUCHED", held

            if hit_sl:
                return sl, -1.0, "SL", held

            if hit_tp:
                result = (entry - tp) / risk
                return tp, result, "TP", held

    return None


def analyze_historical(
    symbol,
    timeframe,
    candles,
    h1_all,
    h4_all,
    d1_all,
    index
):

    signal_candle = candles[index]

    signal_close = (
        candle_timestamp(signal_candle)
        + DURATION_MS[timeframe]
    )

    # Faqat signal paytigacha yopilgan yuqori TF shamlar.
    h1 = closed_candles(h1_all, "H1", signal_close)
    h4 = closed_candles(h4_all, "H4", signal_close)
    d1 = closed_candles(d1_all, "D1", signal_close)

    entry_history = candles[:index + 1]

    minimum = bot_v5.MIN_CANDLES

    if min(
        len(entry_history),
        len(h1),
        len(h4),
        len(d1)
    ) < minimum:
        return None

    # V5.1 analizatoriga aynan o'sha vaqtdagi D1 ma'lumotini beramiz.
    bot_v5._D1_CACHE[symbol] = d1

    return bot_v5.analyze_v5(
        symbol,
        timeframe,
        entry_history,
        h1,
        h4
    )


def backtest():

    all_trades = []

    # V5.1 dagi mavjud instrumentlar.
    # NASDAQ va BRENT qayta qo'shilmaydi.
    symbols = [
        symbol for symbol in core.SYMBOLS
        if symbol not in ("NAS100", "NASDAQ", "NASDAQ100", "BRENT")
    ]

    logging.info("BACKTEST BOSHLANDI")
    logging.info("Instrumentlar: %s", symbols)

    for symbol in symbols:

        logging.info("%s ma'lumotlari yuklanmoqda...", symbol)

        data = {}

        try:

            for timeframe, interval in core.TIMEFRAMES.items():
                data[timeframe] = core.get_candles(
                    symbol,
                    interval
                )

            data["D1"] = core.get_candles(symbol, "1d")

        except Exception as error:

            logging.exception(
                "%s ma'lumotlarini yuklashda xato: %s",
                symbol,
                error
            )
            continue

        h1_all = data.get("H1", [])
        h4_all = data.get("H4", [])
        d1_all = data.get("D1", [])

        for timeframe in core.TIMEFRAMES:

            candles = data.get(timeframe, [])

            if len(candles) < bot_v5.MIN_CANDLES + 2:

                logging.warning(
                    "%s %s: shamlar yetarli emas (%s)",
                    symbol,
                    timeframe,
                    len(candles)
                )
                continue

            start = max(
                bot_v5.MIN_CANDLES - 1,
                len(candles) - MAX_TEST_BARS - 1
            )

            end = len(candles) - 1

            logging.info(
                "%s %s: tarixiy tahlil boshlandi",
                symbol,
                timeframe
            )

            # Bir vaqtning o'zida faqat bitta ochiq savdo.
            next_allowed_index = start

            for i in range(start, end):

                if i < next_allowed_index:
                    continue

                try:

                    signal = analyze_historical(
                        symbol,
                        timeframe,
                        candles,
                        h1_all,
                        h4_all,
                        d1_all,
                        i
                    )

                    if not signal:
                        continue

                    # Signal shamidan keyingi sham ochilishida kirish.
                    entry_candle = candles[i + 1]

                    entry = float(entry_candle["open"])
                    side = signal["side"]
                    sl = float(signal["sl"])
                    tp = float(signal["tp1"])

                    if side == "BUY":

                        if not sl < entry < tp:
                            continue

                    elif side == "SELL":

                        if not tp < entry < sl:
                            continue

                    else:
                        continue

                    risk = abs(entry - sl)

                    if risk <= 0:
                        continue

                    actual_rr = abs(tp - entry) / risk

                    if actual_rr < bot_v5.CONFIG.min_rr:
                        continue

                    future = candles[
                        i + 1:
                        min(
                            len(candles),
                            i + 1 + MAX_HOLDING_BARS
                        )
                    ]

                    outcome = calculate_exit(
                        side,
                        entry,
                        sl,
                        tp,
                        future
                    )

                    base = {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "signal_time": candles[i].get("time", ""),
                        "side": side,
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "rr": round(actual_rr, 3),
                        "score": signal.get("score", ""),
                    }

                    if outcome is None:

                        all_trades.append({
                            **base,
                            "exit_time": "",
                            "exit_price": "",
                            "result_r": "",
                            "outcome": "OPEN_UNRESOLVED",
                            "bars_held": len(future),
                        })

                        # Ochiq savdo qolgan davrda yangi savdo ochmaymiz.
                        next_allowed_index = end
                        continue

                    exit_price, result_r, reason, bars_held = outcome

                    exit_index = i + bars_held

                    exit_time = candles[exit_index].get("time", "")

                    all_trades.append({
                        **base,
                        "exit_time": exit_time,
                        "exit_price": exit_price,
                        "result_r": round(result_r, 4),
                        "outcome": reason,
                        "bars_held": bars_held,
                    })

                    # Savdo yopilganidan keyingi shamdan davom etamiz.
                    next_allowed_index = exit_index + 1

                except Exception:

                    logging.exception(
                        "%s %s index=%s xatosi",
                        symbol,
                        timeframe,
                        i
                    )

    fields = [
        "symbol",
        "timeframe",
        "signal_time",
        "side",
        "entry",
        "sl",
        "tp",
        "rr",
        "score",
        "exit_time",
        "exit_price",
        "result_r",
        "outcome",
        "bars_held",
    ]

    with open(
        OUTPUT_FILE,
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields
        )

        writer.writeheader()
        writer.writerows(all_trades)

    completed = [
        trade for trade in all_trades
        if isinstance(trade["result_r"], (int, float))
    ]

    wins = [
        trade for trade in completed
        if trade["result_r"] > 0
    ]

    losses = [
        trade for trade in completed
        if trade["result_r"] < 0
    ]

    gross_profit = sum(
        trade["result_r"] for trade in wins
    )

    gross_loss = abs(sum(
        trade["result_r"] for trade in losses
    ))

    net_r = sum(
        trade["result_r"] for trade in completed
    )

    win_rate = (
        len(wins) / len(completed) * 100
        if completed else 0
    )

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else (float("inf") if gross_profit > 0 else 0)
    )

    # Drawdown hisoblash uchun savdolarni yopilish vaqti bo'yicha tartiblaymiz.
    chronological = sorted(
        completed,
        key=lambda trade: trade["exit_time"]
    )

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0

    for trade in chronological:

        equity += trade["result_r"]
        peak = max(peak, equity)

        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)

    unresolved = sum(
        trade["outcome"] == "OPEN_UNRESOLVED"
        for trade in all_trades
    )

    print("\n========== V5.1 BACKTEST ==========")
    print("Jami savdolar:", len(all_trades))
    print("Yopilgan savdolar:", len(completed))
    print("Ochiq/qolib ketgan:", unresolved)
    print("Yutuqlar:", len(wins))
    print("Zararlar:", len(losses))
    print(f"Win rate: {win_rate:.2f}%")
    print(f"Net result: {net_r:.2f}R")

    if completed:
        print(
            "O'rtacha natija:",
            f"{net_r / len(completed):.3f}R"
        )

    if math.isfinite(profit_factor):
        print(f"Profit Factor: {profit_factor:.3f}")
    else:
        print("Profit Factor: Infinity")

    print(f"Max Drawdown: {max_drawdown:.2f}R")

    print("\nCSV fayl:", OUTPUT_FILE)

    print(
        "\nDIQQAT: Spread, komissiya, swap va real slippage "
        "hisobga olinmagan. Natijalar kafolat emas."
    )

    # Instrument va timeframe bo'yicha natijalar.
    groups = {}

    for trade in completed:

        key = (
            trade["symbol"],
            trade["timeframe"]
        )

        groups.setdefault(key, []).append(
            trade["result_r"]
        )

    print("\n====== INSTRUMENT / TIMEFRAME ======")

    for key, values in sorted(groups.items()):

        positive = sum(x for x in values if x > 0)
        negative = abs(sum(x for x in values if x < 0))

        pf = (
            positive / negative
            if negative > 0
            else (float("inf") if positive > 0 else 0)
        )

        wr = (
            sum(x > 0 for x in values)
            / len(values) * 100
        )

        print(
            f"{key[0]:8s} {key[1]:4s} | "
            f"N={len(values):3d} | "
            f"Win={wr:5.1f}% | "
            f"Net={sum(values):7.2f}R | "
            f"PF={pf:.2f}"
        )


if __name__ == "__main__":
    backtest()
          
