
"""
V5.2 HISTORICAL BACKTEST
- bot_v5.py o'zgartirilmaydi.
- M15/M30/H1/H4: 180 kunlik tarix.
- D1: 500 kunlik tarix.
- Har timeframe uchun oxirgi 1000 tagacha sham tekshiriladi.
- BTCUSD: Binance tarixiy ma'lumotlari.
- Ochiq qolgan bitimlar oxirgi mavjud narxda baholanadi.
- Signal strategiyasi va V5.1 filtrlari o'zgartirilmaydi.
"""

import os
import csv
import json
import math
import logging
import subprocess
from datetime import datetime, timedelta, timezone

import requests

# bot_v4 import uchun kerakli muhit qiymatlari
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
# SOZLAMALAR
# =========================================================

DURATION_MS = {
    "M15": 15 * 60 * 1000,
    "M30": 30 * 60 * 1000,
    "H1": 60 * 60 * 1000,
    "H4": 4 * 60 * 60 * 1000,
    "D1": 24 * 60 * 60 * 1000,
}

TIMEFRAME_MAP = {
    "M15": ("15m", "m15"),
    "M30": ("30m", "m30"),
    "H1": ("1h", "h1"),
    "H4": ("4h", "h4"),
    "D1": ("1d", "d1"),
}

INTRADAY_HISTORY_DAYS = 180
DAILY_HISTORY_DAYS = 500

MAX_TEST_BARS = 1000
MAX_HOLDING_BARS = 100

OUTPUT_FILE = "backtest_v5_results.csv"
SUMMARY_FILE = "backtest_v5_summary.csv"

# Broker xarajatlari brokerga qarab farq qiladi.
# BACKTEST_COST_R muhit qiymati orqali har bir bitim
# uchun jami xarajatni R birlikda kiritish mumkin.
# Masalan: 0.05 = har bir bitimga 0.05R xarajat.
COST_R_PER_TRADE = float(
    os.getenv("BACKTEST_COST_R", "0")
)

EXCLUDED_SYMBOLS = {
    "NAS100",
    "NASDAQ",
    "NASDAQ100",
    "BRENT",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

DIAGNOSTICS = {}


# =========================================================
# DIAGNOSTIKA
# =========================================================

def get_stats(symbol, timeframe):
    key = (symbol, timeframe)

    if key not in DIAGNOSTICS:
        DIAGNOSTICS[key] = {
            "tested": 0,
            "signals": 0,
            "rejected": 0,
            "errors": 0,
            "insufficient": 0,
            "invalid_levels": 0,
            "reasons": {},
        }

    return DIAGNOSTICS[key]


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


bot_v5._reject = diagnostic_reject


# =========================================================
# SHAM YORDAMCHI FUNKSIYALARI
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
            candle["low"] <= candle["open"] <= candle["high"]
        ):
            return None

        if not (
            candle["low"] <= candle["close"] <= candle["high"]
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
# DUKASCOPY TARIXI
# =========================================================

def fetch_dukascopy_history(symbol, timeframe, days):
    if timeframe not in TIMEFRAME_MAP:
        raise ValueError(
            f"Noto'g'ri timeframe: {timeframe}"
        )

    _, dukascopy_tf = TIMEFRAME_MAP[timeframe]

    dukascopy_symbol = core.DUKASCOPY_SYMBOLS.get(symbol)

    if not dukascopy_symbol:
        raise RuntimeError(
            f"Dukascopy kodi topilmadi: {symbol}"
        )

    now = datetime.now(timezone.utc)

    from_date = (
        now - timedelta(days=days)
    ).strftime("%Y-%m-%d")

    to_date = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    output_dir = os.path.abspath("download")
    os.makedirs(output_dir, exist_ok=True)

    file_name = (
        f"v52_{dukascopy_symbol}_{dukascopy_tf}_"
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
        "-i", dukascopy_symbol,
        "-from", from_date,
        "-to", to_date,
        "-t", dukascopy_tf,
        "-f", "json",
        "-dir", output_dir,
        "-fn", file_name,
        "--cache",
        "--retries", "3",
    ]

    logging.info(
        "%s %s: Dukascopy'dan %s kunlik tarix yuklanmoqda",
        symbol,
        timeframe,
        days
    )

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=900
    )

    if result.returncode != 0:
        diagnostic = "\n".join(
            x for x in (result.stdout, result.stderr)
            if x and x.strip()
        )

        raise RuntimeError(
            "Dukascopy xatosi: "
            + (diagnostic[-3000:] or "CLI xabar bermadi")
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
    duration = DURATION_MS[timeframe]

    candles_by_time = {}

    for item in raw_data:
        try:
            timestamp = int(item["timestamp"])

            if timestamp + duration > now_ms:
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
            f"{symbol} {timeframe}: "
            f"shamlar yetarli emas: {len(candles)}"
        )

    logging.info(
        "%s %s: %s ta sham olindi",
        symbol,
        timeframe,
        len(candles)
    )

    return candles


# =========================================================
# BTC BINANCE TARIXI
# =========================================================

def fetch_btc_history(timeframe, days):
    interval_map = {
        "M15": "15m",
        "M30": "30m",
        "H1": "1h",
        "H4": "4h",
        "D1": "1d",
    }

    interval = interval_map[timeframe]
    duration = DURATION_MS[timeframe]

    now_ms = int(
        datetime.now(timezone.utc).timestamp() * 1000
    )

    start_ms = now_ms - days * 24 * 60 * 60 * 1000

    cursor = start_ms
    candles_by_time = {}

    url = "https://data-api.binance.vision/api/v3/klines"

    logging.info(
        "BTCUSD %s: Binance'dan %s kunlik tarix yuklanmoqda",
        timeframe,
        days
    )

    session = requests.Session()

    while cursor < now_ms:
        params = {
            "symbol": "BTCUSDT",
            "interval": interval,
            "startTime": cursor,
            "endTime": now_ms,
            "limit": 1000,
        }

        response = session.get(
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
        "BTCUSD %s: %s ta sham olindi",
        timeframe,
        len(candles)
    )

    return candles


def fetch_history(symbol, timeframe):
    days = (
        DAILY_HISTORY_DAYS
        if timeframe == "D1"
        else INTRADAY_HISTORY_DAYS
    )

    if symbol == "BTCUSD":
        return fetch_btc_history(timeframe, days)

    return fetch_dukascopy_history(
        symbol,
        timeframe,
        days
    )


# =========================================================
# TARIXIY SIGNAL TAHLILI
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

    if (
        len(entry_history) < minimum
        or len(h1) < minimum
        or len(h4) < minimum
        or len(d1) < minimum
    ):
        stats["insufficient"] += 1
        return None

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
# BITIM NATIJASINI HISOBLASH
# =========================================================

def calculate_exit(
    side,
    entry,
    sl,
    tp,
    future,
    risk
):
    """
    SL va TP bitta shamda tegsa, ehtiyotkorlik uchun
    SL birinchi deb hisoblanadi.

    100 sham ichida TP/SL tegmasa, oxirgi yopilish
    narxida TIME_EXIT yoki DATA_END bilan baholanadi.
    """

    if risk <= 0:
        return None

    for held, candle in enumerate(future, start=1):
        open_price = float(candle["open"])
        high = float(candle["high"])
        low = float(candle["low"])

        if side == "BUY":
            if open_price <= sl:
                result = (open_price - entry) / risk
                return (
                    open_price,
                    result,
                    "SL_GAP",
                    held
                )

            hit_sl = low <= sl
            hit_tp = high >= tp

            if hit_sl and hit_tp:
                return sl, -1.0, "SL_BOTH_TOUCHED", held

            if hit_sl:
                return sl, -1.0, "SL", held

            if hit_tp:
                return tp, (tp - entry) / risk, "TP", held

        elif side == "SELL":
            if open_price >= sl:
                result = (entry - open_price) / risk
                return (
                    open_price,
                    result,
                    "SL_GAP",
                    held
                )

            hit_sl = high >= sl
            hit_tp = low <= tp

            if hit_sl and hit_tp:
                return sl, -1.0, "SL_BOTH_TOUCHED", held

            if hit_sl:
                return sl, -1.0, "SL", held

            if hit_tp:
                return tp, (entry - tp) / risk, "TP", held

    if not future:
        return None

    last_candle = future[-1]
    exit_price = float(last_candle["close"])

    if side == "BUY":
        result = (exit_price - entry) / risk
    else:
        result = (entry - exit_price) / risk

    if len(future) >= MAX_HOLDING_BARS:
        reason = "TIME_EXIT"
    else:
        reason = "DATA_END"

    return (
        exit_price,
        result,
        reason,
        len(future)
    )


# =========================================================
# BITIMLARNI TEST QILISH
# =========================================================

def run_symbol_backtest(symbol):
    trades = []

    data = {}

    # Barcha timeframe'larni tarixiy manbadan olamiz.
    for timeframe in (
        "M15",
        "M30",
        "H1",
        "H4",
        "D1",
    ):
        try:
            data[timeframe] = fetch_history(
                symbol,
                timeframe
            )
        except Exception:
            logging.exception(
                "%s %s tarixini yuklashda xato",
                symbol,
                timeframe
            )
            return trades

    h1_all = data["H1"]
    h4_all = data["H4"]
    d1_all = data["D1"]

    logging.info(
        "%s TARIX: M15=%s M30=%s H1=%s H4=%s D1=%s",
        symbol,
        len(data["M15"]),
        len(data["M30"]),
        len(h1_all),
        len(h4_all),
        len(d1_all)
    )

    for timeframe in ("M15", "M30", "H1", "H4"):
        candles = data[timeframe]

        if len(candles) < bot_v5.MIN_CANDLES + 2:
            logging.warning(
                "%s %s: shamlar yetarli emas",
                symbol,
                timeframe
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
                    valid = sl < entry < tp
                elif side == "SELL":
                    valid = tp < entry < sl
                else:
                    valid = False

                if not valid:
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
                    future,
                    risk
                )

                if outcome is None:
                    continue

                exit_price, gross_r, reason, bars_held = outcome

                cost_r = COST_R_PER_TRADE
                net_r = gross_r - cost_r

                exit_index = i + bars_held

                exit_index = min(
                    exit_index,
                    len(candles) - 1
                )

                trades.append({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "signal_time": candles[i].get("time", ""),
                    "side": side,
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "rr": round(actual_rr, 3),
                    "score": signal.get("score", ""),
                    "exit_time": candles[exit_index].get("time", ""),
                    "exit_price": exit_price,
                    "gross_r": round(gross_r, 4),
                    "cost_r": round(cost_r, 4),
                    "net_r": round(net_r, 4),
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
                    "%s %s index=%s tahlil xatosi",
                    symbol,
                    timeframe,
                    i
                )

    return trades


# =========================================================
# NATIJALARNI HISOBLASH
# =========================================================

def calculate_metrics(trades):
    if not trades:
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "gross_r": 0,
            "cost_r": 0,
            "net_r": 0,
            "profit_factor": 0,
            "max_drawdown": 0,
        }

    wins = [
        t for t in trades
        if t["net_r"] > 0
    ]

    losses = [
        t for t in trades
        if t["net_r"] < 0
    ]

    gross_profit = sum(
        t["net_r"] for t in wins
    )

    gross_loss = abs(sum(
        t["net_r"] for t in losses
    ))

    net_r = sum(
        t["net_r"] for t in trades
    )

    pf = (
        gross_profit / gross_loss
        if gross_loss > 0
        else (
            float("inf")
            if gross_profit > 0
            else 0
        )
    )

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0

    for trade in sorted(
        trades,
        key=lambda x: x["exit_time"]
    ):
        equity += trade["net_r"]
        peak = max(peak, equity)

        max_drawdown = max(
            max_drawdown,
            peak - equity
        )

    return {
        "count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "gross_r": sum(t["gross_r"] for t in trades),
        "cost_r": sum(t["cost_r"] for t in trades),
        "net_r": net_r,
        "profit_factor": pf,
        "max_drawdown": max_drawdown,
    }


# =========================================================
# ASOSIY BACKTEST
# =========================================================

def backtest():
    all_trades = []

    symbols = [
        symbol for symbol in core.SYMBOLS
        if symbol not in EXCLUDED_SYMBOLS
    ]

    logging.info("======================================")
    logging.info("V5.2 BACKTEST BOSHLANDI")
    logging.info("Instrumentlar: %s", symbols)
    logging.info(
        "Intraday tarix: %s kun",
        INTRADAY_HISTORY_DAYS
    )
    logging.info(
        "D1 tarix: %s kun",
        DAILY_HISTORY_DAYS
    )
    logging.info(
        "Har timeframe test limiti: %s sham",
        MAX_TEST_BARS
    )
    logging.info(
        "Har bitim xarajati: %.4fR",
        COST_R_PER_TRADE
    )
    logging.info("======================================")

    for symbol in symbols:
        logging.info("")
        logging.info("========== %s ==========", symbol)

        trades = run_symbol_backtest(symbol)
        all_trades.extend(trades)

        logging.info(
            "%s: %s ta bitim natijasi",
            symbol,
            len(trades)
        )

    # CSV — barcha bitimlar
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
        "gross_r",
        "cost_r",
        "net_r",
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

    # Umumiy natija
    metrics = calculate_metrics(all_trades)

    print("\n========== V5.2 NATIJALARI ==========")
    print("Jami bitimlar:", metrics["count"])
    print("Yutuqlar:", metrics["wins"])
    print("Zararlar:", metrics["losses"])
    print(f"Win rate: {metrics['win_rate']:.2f}%")
    print(f"Gross natija: {metrics['gross_r']:.2f}R")
    print(f"Xarajatlar: {metrics['cost_r']:.2f}R")
    print(f"Net natija: {metrics['net_r']:.2f}R")

    if math.isfinite(metrics["profit_factor"]):
        print(
            f"Profit Factor: {metrics['profit_factor']:.3f}"
        )
    else:
        print("Profit Factor: Infinity")

    print(
        f"Max Drawdown: {metrics['max_drawdown']:.2f}R"
    )

    # Instrument va timeframe bo'yicha natijalar
    groups = {}

    for trade in all_trades:
        key = (
            trade["symbol"],
            trade["timeframe"]
        )
        groups.setdefault(key, []).append(trade)

    summary_rows = []

    print("\n====== INSTRUMENT / TIMEFRAME ======")

    for key, group in sorted(groups.items()):
        result = calculate_metrics(group)

        row = {
            "symbol": key[0],
            "timeframe": key[1],
            **result,
        }

        summary_rows.append(row)

        print(
            f"{key[0]:8s} {key[1]:4s} | "
            f"N={result['count']:4d} | "
            f"Win={result['win_rate']:5.1f}% | "
            f"Net={result['net_r']:8.2f}R | "
            f"PF={result['profit_factor']:.2f} | "
            f"DD={result['max_drawdown']:.2f}R"
        )

    summary_fields = [
        "symbol",
        "timeframe",
        "count",
        "wins",
        "losses",
        "win_rate",
        "gross_r",
        "cost_r",
        "net_r",
        "profit_factor",
        "max_drawdown",
    ]

    with open(
        SUMMARY_FILE,
        "w",
        newline="",
        encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=summary_fields
        )
        writer.writeheader()
        writer.writerows(summary_rows)

    # Diagnostika
    print("\n========== DIAGNOSTIKA ==========")

    for key, stats in sorted(DIAGNOSTICS.items()):
        print(
            f"{key[0]} {key[1]} | "
            f"Test={stats['tested']} | "
            f"Signal={stats['signals']} | "
            f"Rad={stats['rejected']} | "
            f"Tarix yetishmadi={stats['insufficient']} | "
            f"Xato={stats['errors']}"
        )

        if stats["reasons"]:
            for reason, count in sorted(
                stats["reasons"].items(),
                key=lambda x: x[1],
                reverse=True
            ):
                print(f"  {count} marta: {reason}")

    print("\nCSV fayllar:")
    print(OUTPUT_FILE)
    print(SUMMARY_FILE)

    if COST_R_PER_TRADE == 0:
        print(
            "\nOGOHLANTIRISH: xarajat 0R qilib turibdi. "
            "Broker spredi, komissiyasi va slippage "
            "kiritilmagan. BACKTEST_COST_R orqali "
            "har bitim xarajatini R birlikda sozlang."
        )

    print(
        "\nEslatma: tarixiy natija kelajakdagi foydani "
        "kafolatlamaydi. Bu backtest jonli signal "
        "botining kodini o'zgartirmaydi."
    )


if __name__ == "__main__":
    backtest()
