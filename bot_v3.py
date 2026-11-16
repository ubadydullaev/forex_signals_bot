
import os
import json
import base64
import logging
from datetime import datetime, timezone

import requests

# =============== CONFIG ===============

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = os.environ["TELEGRAM_CHAT_ID"]
WORKER_URL = os.environ["CLOUDFLARE_WORKER_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
GITHUB_BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

SYMBOLS = ["EURUSD", "XAUUSD"]

TIMEFRAMES = {
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
}

ATR_PERIOD = 14
SUPERTREND_FACTOR = 2.5
SL_ATR_MULTIPLIER = 1.5

# Ball 100 foizlik tizimda hisoblanadi
MIN_SCORE = 65

# Hozircha test rejimi. Telegramga xabar yuborilmaydi.
TEST_MODE = True

API_URL = "https://biquote.io/api/{symbol}/ohlc"
STATE_FILE = "bot_v3_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "ForexSignalsBot/3.0"
})

candle_cache = {}


# =============== GITHUB STATE ===============

def load_state():
    url = (
        "https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/contents/{STATE_FILE}"
    )

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    try:
        response = session.get(
            url,
            headers=headers,
            params={"ref": GITHUB_BRANCH},
            timeout=20,
        )

        if response.status_code == 404:
            return {"signals": {}}

        response.raise_for_status()
        result = response.json()

        content = base64.b64decode(
            result["content"]
        ).decode("utf-8")

        return json.loads(content)

    except Exception:
        logging.exception("State yuklashda xato")
        return {"signals": {}}


# =============== MARKET DATA ===============

def get_candles(symbol, interval):
    cache_key = (symbol, interval)

    if cache_key in candle_cache:
        return candle_cache[cache_key]

    url = API_URL.format(symbol=symbol)

    response = session.get(
        url,
        params={"interval": interval, "limit": 300},
        timeout=25,
    )

    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, dict):
        bars = payload.get(
            "bars",
            payload.get("data", payload.get("candles", []))
        )
    else:
        bars = payload

    if not isinstance(bars, list):
        raise ValueError("API candles ro'yxatini bermadi")

    candles = []

    for item in bars:
        if not isinstance(item, dict):
            continue

        if item.get("isOpen") is True:
            continue

        try:
            candles.append({
                "time": str(
                    item.get(
                        "openTime",
                        item.get("timestamp", item.get("time", ""))
                    )
                ),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            })

        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["time"])

    if len(candles) < 60:
        raise ValueError(
            f"{symbol} {interval}: shamlar yetarli emas: "
            f"{len(candles)}"
        )

    candle_cache[cache_key] = candles

    logging.info(
        f"Candles: {symbol} {interval} = {len(candles)}"
    )

    return candles


# =============== EMA ===============

def calculate_ema_series(values, period):
    result = [None] * len(values)

    if len(values) < period:
        return result

    ema = sum(values[:period]) / period
    result[period - 1] = ema

    multiplier = 2 / (period + 1)

    for i in range(period, len(values)):
        ema = (
            (values[i] - ema) * multiplier
            + ema
        )
        result[i] = ema

    return result


# =============== RSI ===============

def calculate_rsi(values, period=14):
    result = [None] * len(values)

    if len(values) <= period:
        return result

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def rsi_value(gain, loss):
        if gain == 0 and loss == 0:
            return 50.0
        if loss == 0:
            return 100.0

        rs = gain / loss
        return 100 - (100 / (1 + rs))

    result[period] = rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i - 1]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i - 1]
        ) / period

        result[i] = rsi_value(avg_gain, avg_loss)

    return result


# =============== ATR ===============

def calculate_atr(candles, period=14):
    tr = []

    for i, candle in enumerate(candles):
        if i == 0:
            value = candle["high"] - candle["low"]
        else:
            previous_close = candles[i - 1]["close"]

            value = max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close),
            )

        tr.append(value)

    result = [None] * len(candles)

    if len(tr) < period:
        return result

    result[period - 1] = sum(tr[:period]) / period

    for i in range(period, len(tr)):
        result[i] = (
            result[i - 1] * (period - 1) + tr[i]
        ) / period

    return result


# =============== ADX ===============

def calculate_adx(candles, period=14):
    size = len(candles)

    tr = [0.0] * size
    plus_dm = [0.0] * size
    minus_dm = [0.0] * size

    for i in range(1, size):
        high_move = (
            candles[i]["high"] - candles[i - 1]["high"]
        )

        low_move = (
            candles[i - 1]["low"] - candles[i]["low"]
        )

        plus_dm[i] = (
            high_move
            if high_move > low_move and high_move > 0
            else 0.0
        )

        minus_dm[i] = (
            low_move
            if low_move > high_move and low_move > 0
            else 0.0
        )

        tr[i] = max(
            candles[i]["high"] - candles[i]["low"],
            abs(
                candles[i]["high"]
                - candles[i - 1]["close"]
            ),
            abs(
                candles[i]["low"]
                - candles[i - 1]["close"]
            ),
        )

    result = [None] * size

    if size <= period * 2:
        return result

    smooth_tr = sum(tr[1:period + 1])
    smooth_plus = sum(plus_dm[1:period + 1])
    smooth_minus = sum(minus_dm[1:period + 1])

    dx_values = [None] * size

    for i in range(period, size):
        if i > period:
            smooth_tr = (
                smooth_tr - smooth_tr / period + tr[i]
            )

            smooth_plus = (
                smooth_plus
                - smooth_plus / period
                + plus_dm[i]
            )

            smooth_minus = (
                smooth_minus
                - smooth_minus / period
                + minus_dm[i]
            )

        if smooth_tr <= 0:
            continue

        plus_di = 100 * smooth_plus / smooth_tr
        minus_di = 100 * smooth_minus / smooth_tr

        total = plus_di + minus_di

        if total > 0:
            dx_values[i] = (
                100 * abs(plus_di - minus_di) / total
            )

    first = period * 2 - 1

    initial = [
        x for x in dx_values[period:first + 1]
        if x is not None
    ]

    if len(initial) < period:
        return result

    adx = sum(initial) / period
    result[first] = adx

    for i in range(first + 1, size):
        if dx_values[i] is not None:
            adx = (
                adx * (period - 1) + dx_values[i]
            ) / period

            result[i] = adx

    return result


# =============== SUPERTREND ===============

def calculate_supertrend(candles):
    atr = calculate_atr(candles, ATR_PERIOD)

    upper = [None] * len(candles)
    lower = [None] * len(candles)
    direction = [None] * len(candles)

    for i in range(len(candles)):
        if atr[i] is None:
            continue

        middle = (
            candles[i]["high"] + candles[i]["low"]
        ) / 2

        basic_upper = (
            middle + SUPERTREND_FACTOR * atr[i]
        )

        basic_lower = (
            middle - SUPERTREND_FACTOR * atr[i]
        )

        if i == ATR_PERIOD - 1:
            upper[i] = basic_upper
            lower[i] = basic_lower
            direction[i] = 1
            continue

        if upper[i - 1] is None:
            continue

        previous_close = candles[i - 1]["close"]

        upper[i] = (
            basic_upper
            if basic_upper < upper[i - 1]
            or previous_close > upper[i - 1]
            else upper[i - 1]
        )

        lower[i] = (
            basic_lower
            if basic_lower > lower[i - 1]
            or previous_close < lower[i - 1]
            else lower[i - 1]
        )

        if direction[i - 1] == -1:
            direction[i] = (
                1
                if candles[i]["close"] > upper[i]
                else -1
            )
        else:
            direction[i] = (
                -1
                if candles[i]["close"] < lower[i]
                else 1
            )

    return direction, atr


# =============== SNAPSHOT ===============

def get_snapshot(symbol, timeframe, interval):
    candles = get_candles(symbol, interval)
    closes = [c["close"] for c in candles]

    ema50 = calculate_ema_series(closes, 50)
    ema200 = calculate_ema_series(closes, 200)
    rsi = calculate_rsi(closes, 14)
    adx = calculate_adx(candles, 14)
    direction, atr = calculate_supertrend(candles)

    return {
        "candles": candles,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi,
        "adx": adx,
        "direction": direction,
        "atr": atr,
        "timeframe": timeframe,
    }


# =============== SCORE ===============

def calculate_score(side, snapshot, h1, h4, index=-1):
    candles = snapshot["candles"]

    price = candles[index]["close"]

    ema50 = snapshot["ema50"][index]
    ema200 = snapshot["ema200"][index]
    rsi = snapshot["rsi"][index]
    adx = snapshot["adx"][index]
    direction = snapshot["direction"][index]

    if any(x is None for x in [
        ema50, rsi, adx, direction
    ]):
        return 0

    expected = 1 if side == "BUY" else -1

    score = 0
    possible = 0

    # Supertrend: 20 ball
    possible += 20
    if direction == expected:
        score += 20

    # EMA50: 15 ball
    possible += 15
    if (
        side == "BUY" and price > ema50
    ) or (
        side == "SELL" and price < ema50
    ):
        score += 15

    # EMA200: faqat mavjud bo'lsa
    if ema200 is not None:
        possible += 15

        if (
            side == "BUY" and ema50 > ema200
        ) or (
            side == "SELL" and ema50 < ema200
        ):
            score += 15

    # RSI: 10 ball
    possible += 10

    if (
        side == "BUY" and 50 < rsi < 75
    ) or (
        side == "SELL" and 25 < rsi < 50
    ):
        score += 10

    # ADX: 10 ball
    possible += 10

    if adx >= 15:
        score += 10

    # H1 va H4 tasdig'i
    for higher in (h1, h4):
        hi_direction = higher["direction"][-1]
        hi_ema50 = higher["ema50"][-1]
        hi_price = higher["candles"][-1]["close"]

        if hi_direction is not None:
            possible += 10

            if hi_direction == expected:
                score += 10

        if hi_ema50 is not None:
            possible += 5

            if (
                side == "BUY" and hi_price > hi_ema50
            ) or (
                side == "SELL" and hi_price < hi_ema50
            ):
                score += 5

    if possible == 0:
        return 0

    return round(score / possible * 100)


# =============== BUILD SIGNAL ===============

def build_signal(symbol, timeframe, interval, h1, h4):
    snapshot = get_snapshot(symbol, timeframe, interval)

    candles = snapshot["candles"]

    current_direction = snapshot["direction"][-1]
    previous_direction = snapshot["direction"][-2]

    if current_direction is None or previous_direction is None:
        return None, "Supertrend hali hisoblanmagan"

    side = "BUY" if current_direction == 1 else "SELL"

    score = calculate_score(side, snapshot, h1, h4)

    if score < MIN_SCORE:
        return None, f"Ball yetarli emas: {score}/100"

    current = candles[-1]
    previous = candles[-2]

    ema_now = snapshot["ema50"][-1]
    ema_previous = snapshot["ema50"][-2]

    rsi_now = snapshot["rsi"][-1]
    rsi_previous = snapshot["rsi"][-2]

    atr = snapshot["atr"][-1]

    if any(x is None for x in [
        ema_now, ema_previous,
        rsi_now, rsi_previous, atr
    ]):
        return None, "Indikator qiymatlari yetarli emas"

    if atr <= 0:
        return None, "ATR noto'g'ri"

    # 1. Supertrend yo'nalishi almashishi
    trend_flip = (
        current_direction != previous_direction
    )

    # 2. EMA50 ga qaytish
    if side == "BUY":
        pullback = (
            previous["low"] <= ema_previous
            and current["close"] > ema_now
        )
    else:
        pullback = (
            previous["high"] >= ema_previous
            and current["close"] < ema_now
        )

    # 3. RSI 50 chizig'ini kesishi
    if side == "BUY":
        rsi_cross = (
            rsi_previous <= 50 and rsi_now > 50
        )
    else:
        rsi_cross = (
            rsi_previous >= 50 and rsi_now < 50
        )

    # 4. RSI qaytish signali
    if side == "BUY":
        rsi_recovery = (
            rsi_previous <= 45 and rsi_now > 45
        )
    else:
        rsi_recovery = (
            rsi_previous >= 55 and rsi_now < 55
        )

    # 5. Oldingi sham maksimum/minimumidan o'tish
    if side == "BUY":
        breakout = (
            current["close"] > previous["high"]
            and 52 < rsi_now < 75
        )
    else:
        breakout = (
            current["close"] < previous["low"]
            and 25 < rsi_now < 48
        )

    setups = []

    if trend_flip:
        setups.append("Trend almashishi")

    if pullback:
        setups.append("EMA50 pullback")

    if rsi_cross:
        setups.append("RSI 50 kesish")

    if rsi_recovery:
        setups.append("RSI qaytishi")

    if breakout:
        setups.append("Momentum breakout")

    if not setups:
        return None, "Signal setup shartlari bajarilmadi"

    entry = current["close"]
    risk = atr * SL_ATR_MULTIPLIER

    if side == "BUY":
        sl = entry - risk
        tp1 = entry + risk
        tp2 = entry + risk * 2
        tp3 = entry + risk * 3
    else:
        sl = entry + risk
        tp1 = entry - risk
        tp2 = entry - risk * 2
        tp3 = entry - risk * 3

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "score": score,
        "rsi": rsi_now,
        "adx": snapshot["adx"][-1],
        "setups": setups,
        "candle_time": current["time"],
    }, "Signal tayyor"


# =============== MESSAGE ===============

def format_price(symbol, value):
    digits = 3 if symbol == "XAUUSD" else 5
    return f"{value:.{digits}f}"


def format_signal(signal):
    symbol = signal["symbol"]

    emoji = "🟢" if signal["side"] == "BUY" else "🔴"

    strength = (
        "KUCHLI"
        if signal["score"] >= 80
        else "O'RTACHA"
    )

    return (
        f"{emoji} <b>{symbol} — {signal['side']}</b>\n"
        f"⏱ Timeframe: <b>{signal['timeframe']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🎯 Entry: <code>{format_price(symbol, signal['entry'])}</code>\n"
        f"🛑 SL: <code>{format_price(symbol, signal['sl'])}</code>\n"
        f"✅ TP1: <code>{format_price(symbol, signal['tp1'])}</code>\n"
        f"✅ TP2: <code>{format_price(symbol, signal['tp2'])}</code>\n"
        f"✅ TP3: <code>{format_price(symbol, signal['tp3'])}</code>\n"
        f"━━━━━━━━━━━━━━\n"
        f"📊 Moslik bahosi: <b>{signal['score']}/100 — {strength}</b>\n"
        f"RSI: {signal['rsi']:.1f} | ADX: {signal['adx']:.1f}\n"
        f"Setup: {', '.join(signal['setups'])}\n"
        f"Strategiya: Supertrend + EMA + RSI + ADX\n"
        f"🕯 Yopilgan sham asosida\n"
        f"⚠️ Avval demo hisobda tekshiring."
    )


# =============== MAIN ===============

def main():
    state = load_state()
    state.setdefault("signals", {})

    sent_count = 0
    signal_count = 0
    errors = []

    for symbol in SYMBOLS:
        try:
            h1 = get_snapshot(symbol, "H1", "1h")
            h4 = get_snapshot(symbol, "H4", "4h")

        except Exception as error:
            logging.exception(f"{symbol} H1/H4 xatosi")
            errors.append(
                f"{symbol} H1/H4: {str(error)[:120]}"
            )
            continue

        for timeframe, interval in TIMEFRAMES.items():
            key = f"{symbol}_{timeframe}"

            try:
                signal, reason = build_signal(
                    symbol,
                    timeframe,
                    interval,
                    h1,
                    h4,
                )

                if signal is None:
                    print(f"INFO | {key} | {reason}")
                    continue

                signature = (
                    f"{signal['candle_time']}_{signal['side']}"
                )

                if state["signals"].get(key) == signature:
                    print(f"SKIP | {key} | Takroriy signal")
                    continue

                signal_count += 1

                print(
                    f"\nSIGNAL | {key} | {signal['side']} "
                    f"| Score: {signal['score']}/100"
                )

                print(format_signal(signal))

                if not TEST_MODE:
                    print(
                        "XAVFSIZLIK: Jonli yuborish rejimi "
                        "hali alohida sozlanishi kerak."
                    )

            except Exception as error:
                logging.exception(f"{key} xatosi")
                errors.append(
                    f"{key}: {str(error)[:120]}"
                )
                print(f"ERROR | {key} | {error}")

    print("\n========== V3 TEST NATIJA ==========")
    print(f"Topilgan signallar: {signal_count}")
    print(f"Yuborilgan signallar: {sent_count}")
    print(f"Xatolar: {len(errors)}")
    print("TEST MODE:", TEST_MODE)
    print("Telegram xabarlari yuborilmadi.")
    print("State fayliga o'zgarish yozilmadi.")

    if errors:
        print("\nXATOLAR:")
        for error in errors:
            print("-", error)


if __name__ == "__main__":
    main()
