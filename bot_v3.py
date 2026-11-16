
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
MIN_SCORE = 65

API_URL = "https://biquote.io/api/{symbol}/ohlc"
STATE_FILE = "bot_v3_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update({"User-Agent": "ForexSignalsBot/3.0"})
candle_cache = {}


# =============== TELEGRAM ===============

def telegram_send(chat_id, message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = session.post(
        url,
        json={
            "chat_id": str(chat_id),
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )

    response.raise_for_status()
    return response.json()


# =============== GITHUB STATE ===============

def github_file_request():
    url = (
        "https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/contents/{STATE_FILE}"
    )

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    return url, headers


def load_state():
    url, headers = github_file_request()

    try:
        response = session.get(
            url,
            headers=headers,
            params={"ref": GITHUB_BRANCH},
            timeout=20,
        )

        if response.status_code == 404:
            return {"signals": {}, "heartbeat_date": ""}, None

        response.raise_for_status()
        result = response.json()

        content = base64.b64decode(result["content"]).decode("utf-8")
        return json.loads(content), result["sha"]

    except Exception:
        logging.exception("V3 state yuklashda xato")
        return {"signals": {}, "heartbeat_date": ""}, None


def save_state(state, sha):
    url, headers = github_file_request()

    content = base64.b64encode(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2
        ).encode("utf-8")
    ).decode("utf-8")

    payload = {
        "message": "Update forex V3 state",
        "content": content,
        "branch": GITHUB_BRANCH,
    }

    if sha:
        payload["sha"] = sha

    response = session.put(
        url,
        headers=headers,
        json=payload,
        timeout=20,
    )

    response.raise_for_status()


# =============== USERS ===============

def get_approved_users():
    response = session.get(
        f"{WORKER_URL}/approved-users",
        headers={"X-Worker-Secret": WEBHOOK_SECRET},
        timeout=20,
    )

    response.raise_for_status()

    users = response.json().get("users", [])

    return list(
        set([str(ADMIN_ID)] + [str(x) for x in users])
    )


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
            f"{symbol} {interval}: indikatorlar uchun "
            f"shamlar yetarli emas ({len(candles)})"
        )

    candle_cache[cache_key] = candles
    logging.info(f"Candles: {symbol} {interval} = {len(candles)}")
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
        high_move = candles[i]["high"] - candles[i - 1]["high"]
        low_move = candles[i - 1]["low"] - candles[i]["low"]

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
            abs(candles[i]["high"] - candles[i - 1]["close"]),
            abs(candles[i]["low"] - candles[i - 1]["close"]),
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
            smooth_tr = smooth_tr - smooth_tr / period + tr[i]
            smooth_plus = (
                smooth_plus - smooth_plus / period + plus_dm[i]
            )
            smooth_minus = (
                smooth_minus - smooth_minus / period + minus_dm[i]
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
                1 if candles[i]["close"] > upper[i]
                else -1
            )
        else:
            direction[i] = (
                -1 if candles[i]["close"] < lower[i]
                else 1
            )

    return direction, atr


# =============== INDICATOR SNAPSHOT ===============

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


# =============== SIGNAL SCORE ===============

def calculate_score(side, snapshot, h1, h4, index=-1):
    candles = snapshot["candles"]

    i = index
    price = candles[i]["close"]

    ema50 = snapshot["ema50"][i]
    ema200 = snapshot["ema200"][i]
    rsi = snapshot["rsi"][i]
    adx = snapshot["adx"][i]
    direction = snapshot["direction"][i]

    if any(x is None for x in [
        ema50, ema200, rsi, adx, direction
    ]):
        return 0

    score = 0

    if (side == "BUY" and direction == 1) or (
        side == "SELL" and direction == -1
    ):
        score += 20

    if (side == "BUY" and price > ema50) or (
        side == "SELL" and price < ema50
    ):
        score += 10

    if (side == "BUY" and ema50 > ema200) or (
        side == "SELL" and ema50 < ema200
    ):
        score += 20

    if (side == "BUY" and 50 < rsi < 75) or (
        side == "SELL" and 25 < rsi < 50
    ):
        score += 10

    if adx >= 15:
        score += 15

    for higher in (h1, h4):
        hi_direction = higher["direction"][-1]
        hi_ema50 = higher["ema50"][-1]
        hi_ema200 = higher["ema200"][-1]

        if hi_direction is not None:
            expected = 1 if side == "BUY" else -1

            if hi_direction == expected:
                score += 5

        if hi_ema50 is not None and hi_ema200 is not None:
            if (side == "BUY" and hi_ema50 > hi_ema200) or (
                side == "SELL" and hi_ema50 < hi_ema200
            ):
                score += 5

    return score


# =============== BUILD SIGNAL ===============

def build_signal(symbol, timeframe, interval, h1, h4):
    snapshot = get_snapshot(symbol, timeframe, interval)

    candles = snapshot["candles"]

    current_direction = snapshot["direction"][-1]
    previous_direction = snapshot["direction"][-2]

    if current_direction is None or previous_direction is None:
        return None

    side = "BUY" if current_direction == 1 else "SELL"

    score = calculate_score(side, snapshot, h1, h4)

    if score < MIN_SCORE:
        return None

    current = candles[-1]
    previous = candles[-2]

    ema_now = snapshot["ema50"][-1]
    ema_previous = snapshot["ema50"][-2]

    rsi_now = snapshot["rsi"][-1]
    rsi_previous = snapshot["rsi"][-2]

    if any(x is None for x in [
        ema_now, ema_previous, rsi_now, rsi_previous
    ]):
        return None

    trend_flip = current_direction != previous_direction

    if side == "BUY":
        pullback = (
            previous["low"] <= ema_previous
            and current["close"] > ema_now
        )

        rsi_cross = (
            rsi_previous <= 50 and rsi_now > 50
        )

    else:
        pullback = (
            previous["high"] >= ema_previous
            and current["close"] < ema_now
        )

        rsi_cross = (
            rsi_previous >= 50 and rsi_now < 50
        )

    if not (trend_flip or pullback or rsi_cross):
        return None

    entry = current["close"]
    atr = snapshot["atr"][-1]

    if atr is None or atr <= 0:
        return None

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
        "candle_time": current["time"],
    }


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
        f"📊 Kuch bahosi: <b>{signal['score']}/90 — {strength}</b>\n"
        f"RSI: {signal['rsi']:.1f} | ADX: {signal['adx']:.1f}\n"
        f"Strategiya: Supertrend + EMA50/200 + RSI + ADX\n"
        f"🕯 Yopilgan sham asosida\n"
        f"⚠️ Avval demo hisobda tekshiring."
    )


# =============== MAIN ===============

def main():
    state, sha = load_state()

    state.setdefault("signals", {})
    state.setdefault("heartbeat_date", "")

    users = []

    sent_count = 0
    errors = []

    for symbol in SYMBOLS:
        try:
            h1 = get_snapshot(symbol, "H1", "1h")
            h4 = get_snapshot(symbol, "H4", "4h")
        except Exception as error:
            logging.exception(f"{symbol} H1/H4 xatosi")
            errors.append(f"{symbol} H1/H4: {str(error)[:120]}")
            continue

        for timeframe, interval in TIMEFRAMES.items():
            key = f"{symbol}_{timeframe}"

            try:
                signal = build_signal(
                    symbol,
                    timeframe,
                    interval,
                    h1,
                    h4,
                )

                if signal is None:
                    print(f"INFO | {key} | Yangi signal yo'q")
                    continue

                signature = (
                    f"{signal['candle_time']}_{signal['side']}"
                )

                if state["signals"].get(key) == signature:
                    print(f"SKIP | {key} | Takroriy signal")
                    continue

                message = format_signal(signal)
                delivered = False

                for user_id in users:
                    try:
                        telegram_send(user_id, message)
                        delivered = True
                    except Exception:
                        logging.exception(
                            f"Telegram yuborishda xato: {user_id}"
                        )

                if delivered:
                    state["signals"][key] = signature
                    sent_count += 1

                    print(
                        f"SENT | {key} | {signal['side']} | "
                        f"Score: {signal['score']}"
                    )
                else:
                    print(f"ERROR | {key} | Xabar yetkazilmadi")

            except Exception as error:
                logging.exception(f"{key} xatosi")
                errors.append(f"{key}: {str(error)[:120]}")
                print(f"ERROR | {key} | {error}")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if False and state["heartbeat_date"] != today:
        try:
            telegram_send(
                ADMIN_ID,
                "🤖 <b>Forex Signals Bot V3 faol</b>\n"
                "EURUSD va XAUUSD tekshirilmoqda.\n"
                "M5 / M15 / M30 / H1 / H4"
            )
            state["heartbeat_date"] = today
        except Exception:
            logging.exception("Heartbeat yuborishda xato")

    print("V3 TEST MODE: Telegram va state saqlash o‘chirildi")

    print("\n========== V3 NATIJA ==========")
    print(f"Yangi signallar: {sent_count}")
    print(f"Xatolar: {len(errors)}")

    if errors:
        try:
            telegram_send(
                ADMIN_ID,
                "⚠️ <b>V3 xatolari:</b>\n"
                + "\n".join(errors[:10])
            )
        except Exception:
            logging.exception("Xatolar haqida xabar yuborilmadi")


if __name__ == "__main__":
    main()

