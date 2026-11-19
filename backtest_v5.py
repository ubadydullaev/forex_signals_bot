
"""
V5.1 HISTORICAL BACKTEST — DIAGNOSTIC VERSION

- bot_v5.py va jonli signal botiga tegmaydi.
- H1/H4 uchun 180 kunlik tarix yuklaydi.
- BTCUSD uchun Binance tarixini sahifalab yuklaydi.
- V5.1 filtrlari o'zgartirilmaydi.
- Rad etilish sabablarini hisoblaydi.
- Telegram xabar yubormaydi.
"""

import os
import csv
import math
import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

import requests


# bot_v4 import vaqtida talab qiladigan qiymatlar.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "BACKTEST_ONLY")
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")
os.environ.setdefault("CLOUDFLARE_WORKER_URL", "https://example.invalid")
os.environ.setdefault("WEBHOOK_SECRET", "BACKTEST_ONLY")
os.environ.setdefault("GITHUB_TOKEN", "BACKTEST_ONLY")
os.environ.setdefault("GITHUB_REPOSITORY", "BACKTEST_ONLY")
os.environ.setdefault("GITHUB_REF_NAME", "main")

import bot_v5

core = bot_v5.core


# =========================================================
# CONFIG
# =========================================================

DURATION_MS = {
    "M15": 15 * 60 * 1000,
    "M30": 30 * 60 * 1000,
    "H1": 60 * 60 * 1000,
    "H4": 4 * 60 * 60 * 1000,
    "D1": 24 * 60 * 60 * 1000,
}

MAX_TEST_BARS = 100
MAX_HOLDING_BARS = 100

EXTENDED_HISTORY_DAYS = 180

OUTPUT_FILE = "backtest_v5_results.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# Har bir instrument va timeframe uchun diagnostika.
DIAGNOSTICS = {}


def get_stats(symbol, timeframe):
    key = (symbol, timeframe)

    if key not in DIAGNOSTICS:
        DIAGNOSTICS[key] = {
            "tested": 0,
            "insufficient_entry": 0,
            "insufficient_h1": 0,
            "insufficient_h4": 0,
            "insufficient_d1": 0,
            "rejected": 0,
            "signals": 0,
            "invalid_levels": 0,
            "errors": 0,
            "reasons": {},
        }

    return DIAGNOSTICS[key]


# =========================================================
# V5 REJECTION DIAGNOSTICS
# =========================================================

_original_reject = bot_v5._reject


def diagnostic_reject(symbol, timeframe, reason):
    stats = get_stats(symbol, timeframe)

    stats["rejected"] += 1

    reason = str(reason)

    stats["reasons"][reason] = (
        stats["reasons"].get(reason, 0) + 1
    )

    return _original_reject(
        symbol,
        timeframe,
        reason
    )


# Faqat backtest jarayonida rad etish sabablarini sanaydi.
bot_v5._reject = diagnostic_reject


# =========================================================
# CANDLE HELPERS
# =========================================================

def candle_timestamp(candle):
    return int(candle.get("timestamp", 0))


def closed_candles(candles, timeframe, cutoff):
    duration = DURATION_MS[timeframe]

    return [
        candle for candle in candles
        if candle_timestamp(candle) + duration <= cutoff
    ]


def validate_candle(item, timestamp):
    try:
        candle = {
            "timestamp": int(timestamp),
            "time": datetime.fromtimestamp(
                int(timestamp) / 1000,
                timezone.utc
            ).isoformat(),
            "open": float(item["open"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "close": float(item["close"]),
        }

        prices = (
            candle["open"],
            candle["high"],
            candle["low"],
            candle["close"],
        )

        if not all(
            math.isfinite(x) and x > 0
            for x in prices
        ):
            return None

        if candle["high"] < candle["low"]:
            return None

        if not (
            candle["low"]
            <= candle["open"]
            <= candle["high"]
        ):
            return None

        if not (
            candle["low"]
            <= candle["close"]
            <= candle["high"]
        ):
            return None

        return candle

    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError
    ):
        return None


# =========================================================
# EXTENDED DUKASCOPY HISTORY
# =========================================================

def fetch_dukascopy_history(symbol, interval, days):
    timeframe_map = {
        "1h": ("h1", 60 * 60 * 1000),
        "4h": ("h4", 4 * 60 * 60 * 1000),
    }

    if interval not in timeframe_map:
        raise ValueError(
            f"Extended timeframe noto'g'ri: {interval}"
        )

    dukascopy_tf, candle_ms = timeframe_map[interval]

    dukascopy_symbol = core.DUKASCOPY_SYMBOLS.get(symbol)

    if not dukascopy_symbol:
        raise RuntimeError(
            f"Dukascopy instrument kodi topilmadi: {symbol}"
        )

    now = datetime.now(timezone.utc)

    from_date = (
        now - timedelta(days=days)
    ).strftime("%Y-%m-%d")

    to_date = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    output_dir = os.path.abspath("download")

    os.makedirs(output_dir, exist_ok=True)

    file_name = (
        f"backtest_{dukascopy_symbol}_{dukascopy_tf}_"
        f"{now.strftime('%Y%m%d%H%M%S')}"
    )

    output_file = os.path.join(
        output_dir,
        file_name + ".json"
    )

    command = [
        "npx",
        "--no-install",
        "dukascopy-node",
        "-i",
        dukascopy_symbol,
        "-from",
        from_date,
        "-to",
        to_date,
        "-t",
        dukascopy_tf,
        "-f",
        "json",
        "-dir",
        output_dir,
        "-fn",
        file_name,
        "--cache",
        "--retries",
        "3",
    ]

    logging.info(
        "%s %s: %s kunlik Dukascopy tarixi yuklanmoqda",
        symbol,
        interval,
        days
    )

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=600
    )

    if result.returncode != 0:
        diagnostic = "\n".join(
            x for x in (
                result.stdout,
                result.stderr
            )
            if x and x.strip()
        )

        raise RuntimeError(
            "Dukascopy xatosi: "
            + (
                diagnostic[-3000:]
                or "CLI xabar qaytarmadi"
            )
        )

    if not os.path.exists(output_file):
        raise RuntimeError(
            "Dukascopy JSON fayli topilmadi"
        )

    with open(
        output_file,
        "r",
        encoding="utf-8"
    ) as file:
        raw_data = json.load(file)

    if not isinstance(raw_data, list):
        raise RuntimeError(
            "Dukascopy noto'g'ri JSON qaytardi"
        )

    now_ms = int(now.timestamp() * 1000)

    candles_by_time = {}

    for item in raw_data:
        try:
            timestamp = int(item["timestamp"])

            # Faqat to'liq yopilgan shamlar.
            if timestamp + candle_ms > now_ms:
                continue

            candle = validate_candle(
                item,
                timestamp
            )

            if candle:
                candles_by_time[timestamp] = candle

        except Exception:
            continue

    candles = sorted(
        candles_by_time.values(),
        key=lambda x: x["timestamp"]
    )

    if len(candles) < bot_v5.MIN_CANDLES:
        raise RuntimeError(
            f"{symbol} {interval}: "
            f"shamlar yetarli emas: {len(candles)}"
        )

    logging.info(
        "%s %s: %s ta tarixiy sham olindi",
        symbol,
        interval,
        len(candles)
    )

    return candles


# =========================================================
# EXTENDED BTC HISTORY — BINANCE
# =========================================================


def fetch_btc_history(interval, days):
    timeframe = interval.upper()

    if timeframe not in DURATION_MS:
        raise ValueError(
            f"BTC timeframe noto'g'ri: {interval}"
        )

    duration = DURATION_MS[timeframe]

    now_ms = int(
        datetime.now(timezone.utc).timestamp() * 1000
    )

    start_ms = (
        now_ms - days * 24 * 60 * 60 * 1000
    )

    cursor = start_ms
    candles_by_time = {}

    url = "https://data-api.binance.vision/api/v3/klines"

    logging.info(
        "BTCUSD %s: Binance tarixiy ma'lumotlari yuklanmoqda",
        timeframe
    )

    while cursor < now_ms:
        params = {
            "symbol": "BTCUSDT",
            "interval": interval.lower(),
            "startTime": cursor,
            "endTime": now_ms,
            "limit": 1000,
        }

        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        response.raise_for_status()

        raw_data = response.json()

        if not isinstance(raw_data, list) or not raw_data:
            break

        last_timestamp = None

        for item in raw_data:
            try:
                timestamp = int(item[0])
                close_time = int(item[6])

                if close_time >= now_ms:
                    continue

                candle = validate_candle(
                    {
                        "open": item[1],
                        "high": item[2],
                        "low": item[3],
                        "close": item[4],
                    },
                    timestamp
                )

                if candle:
                    candles_by_time[timestamp] = candle

                last_timestamp = timestamp

            except Exception:
                continue

        if last_timestamp is None:
            break

        next_cursor = last_timestamp + duration

        if next_cursor <= cursor:
            break

        cursor = next_cursor

    candles = sorted(
        candles_by_time.values(),
        key=lambda x: x["timestamp"]
    )

    if len(candles) < bot_v5.MIN_CANDLES:
        raise RuntimeError(
            f"BTCUSD {timeframe}: "
            f"shamlar yetarli emas: {len(candles)}"
        )

    logging.info(
        "BTCUSD %s: %s ta tarixiy sham olindi",
        timeframe,
        len(candles)
    )

    return candles


def fetch_extended_history(symbol, interval):
    if symbol == "BTCUSD":
        return fetch_btc_history(
            interval,
            EXTENDED_HISTORY_DAYS
        )

    return fetch_dukascopy_history(
        symbol,
        interval,
        EXTENDED_HISTORY_DAYS
    )


# =========================================================
# HISTORICAL ANALYSIS
# =========================================================

def analyze_historical(
    symbol,
    timeframe,
    candles,
    h1_all,
    h4_all,
    d1_all,
    index
):
    stats = get_stats(symbol, timeframe)

    stats["tested"] += 1

    signal_candle = candles[index]

    signal_close = (
        candle_timestamp(signal_candle)
        + DURATION_MS[timeframe]
    )

    # Signal paytigacha to'liq yopilgan shamlar.
    h1 = closed_candles(
        h1_all,
        "H1",
        signal_close
    )

    h4 = closed_candles(
        h4_all,
        "H4",
        signal_close
    )

    d1 = closed_candles(
        d1_all,
        "D1",
        signal_close
    )

    entry_history = candles[:index + 1]

    minimum = bot_v5.MIN_CANDLES

    # Yetishmayotgan tarixni alohida hisoblash.
    if len(entry_history) < minimum:
        stats["insufficient_entry"] += 1
        return None

    if len(h1) < minimum:
        stats["insufficient_h1"] += 1
        return None

    if len(h4) < minimum:
        stats["insufficient_h4"] += 1
        return None

    if len(d1) < minimum:
        stats["insufficient_d1"] += 1
        return None

    # V5.1 analizatoriga o'sha paytdagi D1 ma'lumotini beramiz.
    bot_v5._D1_CACHE[symbol] = d1

    signal = bot_v5.analyze_v5(
        symbol,
        timeframe,
        entry_history,
        h1,
        h4
    )

    if signal:
        stats["signals"] += 1

    return signal


# =========================================================
# TRADE EXIT SIMULATION
# =========================================================

def calculate_exit(side, entry, sl, tp, future):
    """
    Natija:
    exit_price, result_R, reason, bars_held

    Agar bitta shamda SL va TP ikkalasi tegsa,
    ehtiyotkorlik uchun SL birinchi deb olinadi.
    """

    risk = abs(entry - sl)

    if risk <= 0:
        return None

    for held, candle in enumerate(future, start=1):
        open_price = float(candle["open"])
        high = float(candle["high"])
        low = float(candle["low"])

        if side == "BUY":
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


# =========================================================
# BACKTEST
# =========================================================

def backtest():
    all_trades = []

    symbols = [
        symbol for symbol in core.SYMBOLS
        if symbol not in (
            "NAS100",
            "NASDAQ",
            "NASDAQ100",
            "BRENT"
        )
    ]

    logging.info("================================")
    logging.info("V5.1 DIAGNOSTIC BACKTEST BOSHLANDI")
    logging.info("Instrumentlar: %s", symbols)
    logging.info("Tarix: %s kun", EXTENDED_HISTORY_DAYS)
    logging.info("================================")

    for symbol in symbols:
        logging.info("")
        logging.info("========== %s ==========", symbol)

        data = {}

        try:
            for timeframe, interval in core.TIMEFRAMES.items():
                if timeframe in ("H1", "H4"):
                    data[timeframe] = fetch_extended_history(
                        symbol,
                        interval
                    )
                else:
                    data[timeframe] = core.get_candles(
                        symbol,
                        interval
                    )

            # D1 ma'lumoti V5.1 uchun alohida olinadi.
            data["D1"] = core.get_candles(
                symbol,
                "1d"
            )

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

        logging.info(
            "%s: H1=%s | H4=%s | D1=%s",
            symbol,
            len(h1_all),
            len(h4_all),
            len(d1_all)
        )

        for timeframe in core.TIMEFRAMES:
            candles = data.get(timeframe, [])

            if len(candles) < bot_v5.MIN_CANDLES + 2:
                logging.warning(
                    "%s %s: entry shamlar yetarli emas: %s",
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
                "%s %s: %s ta tarixiy nuqta tekshiriladi",
                symbol,
                timeframe,
                end - start
            )

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
                            get_stats(
                                symbol,
                                timeframe
                            )["invalid_levels"] += 1
                            continue

                    elif side == "SELL":
                        if not tp < entry < sl:
                            get_stats(
                                symbol,
                                timeframe
                            )["invalid_levels"] += 1
                            continue

                    else:
                        get_stats(
                            symbol,
                            timeframe
                        )["invalid_levels"] += 1
                        continue

                    risk = abs(entry - sl)

                    if risk <= 0:
                        get_stats(
                            symbol,
                            timeframe
                        )["invalid_levels"] += 1
                        continue

                    actual_rr = abs(tp - entry) / risk

                    if actual_rr < bot_v5.CONFIG.min_rr:
                        get_stats(
                            symbol,
                            timeframe
                        )["invalid_levels"] += 1
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

                        next_allowed_index = end
                        continue

                    exit_price, result_r, reason, bars_held = outcome

                    exit_index = i + bars_held

                    exit_time = candles[exit_index].get(
                        "time",
                        ""
                    )

                    all_trades.append({
                        **base,
                        "exit_time": exit_time,
                        "exit_price": exit_price,
                        "result_r": round(result_r, 4),
                        "outcome": reason,
                        "bars_held": bars_held,
                    })

                    next_allowed_index = exit_index + 1

                except Exception:
                    get_stats(
                        symbol,
                        timeframe
                    )["errors"] += 1

                    logging.exception(
                        "%s %s index=%s xatosi",
                        symbol,
                        timeframe,
                        i
                    )

    # =====================================================
    # SAVE TRADES CSV
    # =====================================================

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

    # =====================================================
    # PERFORMANCE SUMMARY
    # =====================================================

    completed = [
        trade for trade in all_trades
        if isinstance(
            trade["result_r"],
            (int, float)
        )
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
        else (
            float("inf")
            if gross_profit > 0
            else 0
        )
    )

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
        max_drawdown = max(
            max_drawdown,
            drawdown
        )

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

    # =====================================================
    # SYMBOL / TIMEFRAME RESULTS
    # =====================================================

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
        positive = sum(
            x for x in values if x > 0
        )

        negative = abs(sum(
            x for x in values if x < 0
        ))

        pf = (
            positive / negative
            if negative > 0
            else (
                float("inf")
                if positive > 0
                else 0
            )
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

    # =====================================================
    # DIAGNOSTICS
    # =====================================================

    print("\n========== DIAGNOSTIKA ==========")

    for key, stats in sorted(DIAGNOSTICS.items()):
        symbol, timeframe = key

        print(
            f"\n{symbol} {timeframe} | "
            f"Tekshirildi={stats['tested']} | "
            f"Signal={stats['signals']} | "
            f"Rad={stats['rejected']} | "
            f"Xato={stats['errors']}"
        )

        print(
            "Tarix yetishmadi: "
            f"ENTRY={stats['insufficient_entry']} | "
            f"H1={stats['insufficient_h1']} | "
            f"H4={stats['insufficient_h4']} | "
            f"D1={stats['insufficient_d1']}"
        )

        print(
            "Noto'g'ri Entry/SL/TP:",
            stats["invalid_levels"]
        )

        if stats["reasons"]:
            print("Rad etilish sabablari:")

            for reason, count in sorted(
                stats["reasons"].items(),
                key=lambda item: item[1],
                reverse=True
            ):
                print(
                    f"  {count:4d} marta | {reason}"
                )

    print("\nCSV fayl:", OUTPUT_FILE)

    print(
        "\nDIQQAT: Spread, komissiya, swap va slippage "
        "hisobga olinmagan. Backtest natijasi kelajakdagi "
        "foydani kafolatlamaydi."
    )


if __name__ == "__main__":
    backtest()
