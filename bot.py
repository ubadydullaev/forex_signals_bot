
import os
import json
import time
import base64
import logging
from datetime import datetime, timezone

import requests

# ================= CONFIG =================

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
RR_TARGETS = (1.0, 2.0, 3.0)

API_URL = "https://biquote.io/api/{symbol}/ohlc"
STATE_FILE = "bot_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update({"User-Agent": "ForexSignalsBot/2.0"})


# ================= TELEGRAM =================

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


# ================= GITHUB STATE =================

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
        logging.exception("State yuklashda xato")
        return {"signals": {}, "heartbeat_date": ""}, None


def save_state(state, sha):
    url, headers = github_file_request()

    content = base64.b64encode(
        json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")

    payload = {
        "message": "Update forex bot state",
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


# ================= USERS =================

def get_approved_users():
    response = session.get(
        f"{WORKER_URL}/approved-users",
        headers={"X-Worker-Secret": WEBHOOK_SECRET},
        timeout=20,
    )
    response.raise_for_status()

    users = response.json().get("users", [])
    return list(set([str(ADMIN_ID)] + [str(x) for x in users]))


# ================= MARKET DATA =================

def get_candles(symbol, interval):
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
            candle = {
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
            }

            candles.append(candle)

        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["time"])

    if len(candles) < 60:
        raise ValueError(
            f"{symbol} {interval}: yetarli yopilgan sham yo'q "
            f"({len(candles)} ta)"
        )

    return candles


# ================= INDICATORS =================

def calculate_atr(candles, period):
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

    atr = [None] * len(candles)

    if len(tr) <= period:
        return atr

    atr[period - 1] = sum(tr[:period]) / period

    for i in range(period, len(tr)):
        atr[i] = (
            atr[i - 1] * (period - 1) + tr[i]
        ) / period

    return atr


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

        basic_upper = middle + SUPERTREND_FACTOR * atr[i]
        basic_lower = middle - SUPERTREND_FACTOR * atr[i]

        if i == ATR_PERIOD - 1:
            upper[i] = basic_upper
            lower[i] = basic_lower
            direction[i] = 1
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


def calculate_ema(values, period):
    if len(values) < period:
        return None

    ema = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        ema = (value - ema) * multiplier + ema

    return ema


# ================= SIGNAL =================

def build_signal(symbol, timeframe, interval):
    candles = get_candles(symbol, interval)
    directions, atr_values = calculate_supertrend(candles)

    if directions[-1] is None or directions[-2] is None:
        return None

    previous_direction = directions[-2]
    current_direction = directions[-1]

    if previous_direction == current_direction:
        return None

    closes = [c["close"] for c in candles]
    ema50 = calculate_ema(closes, 50)

    if ema50 is None:
        return None

    price = closes[-1]
    atr = atr_values[-1]

    if atr is None or atr <= 0:
        return None

    if current_direction == 1:
        side = "BUY"

        if price <= ema50:
            return None

        entry = price
        sl = entry - atr * SL_ATR_MULTIPLIER
        risk = entry - sl

        tp1 = entry + risk * RR_TARGETS[0]
        tp2 = entry + risk * RR_TARGETS[1]
        tp3 = entry + risk * RR_TARGETS[2]

    else:
        side = "SELL"

        if price >= ema50:
            return None

        entry = price
        sl = entry + atr * SL_ATR_MULTIPLIER
        risk = sl - entry

        tp1 = entry - risk * RR_TARGETS[0]
        tp2 = entry - risk * RR_TARGETS[1]
        tp3 = entry - risk * RR_TARGETS[2]

    candle_time = candles[-1]["time"]

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "candle_time": candle_time,
    }


# ================= MESSAGE =================

def format_price(symbol, value):
    digits = 3 if symbol == "XAUUSD" else 5
    return f"{value:.{digits}f}"


def format_signal(signal):
    symbol = signal["symbol"]

    emoji = "🟢" if signal["side"] == "BUY" else "🔴"

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
        f"📊 Strategiya: Supertrend + EMA50\n"
        f"🕯 Yopilgan sham asosida\n"
        f"⚠️ Demo hisobda tekshiring."
    )


# ================= MAIN =================


def main():
    state, sha = load_state()
    state.setdefault("signals", {})
    state.setdefault("heartbeat_date", "")

    users = get_approved_users()
    sent_count = 0
    errors = []

    print("\n========== TIMEFRAME DIAGNOSTIKA ==========")

    for symbol in SYMBOLS:
        for timeframe, interval in TIMEFRAMES.items():
            key = f"{symbol}_{timeframe}"

            try:
                candles = get_candles(symbol, interval)
                directions, atr_values = calculate_supertrend(candles)

                print(
                    f"OK | {key} | "
                    f"Interval: {interval} | "
                    f"Yopilgan shamlar: {len(candles)} | "
                    f"Oxirgi sham: {candles[-1]['time']}"
                )

                signal = build_signal(symbol, timeframe, interval)

                if signal is None:
                    print(f"INFO | {key} | Hozir yangi signal yo'q")
                    continue

                signature = (
                    f"{signal['candle_time']}_{signal['side']}"
                )

                if state["signals"].get(key) == signature:
                    print(f"SKIP | {key} | Takroriy signal")
                    continue

                message = format_signal(signal)

                for user_id in users:
                    try:
                        telegram_send(user_id, message)
                    except Exception:
                        logging.exception(
                            f"Telegram yuborishda xato: {user_id}"
                        )

                state["signals"][key] = signature
                sent_count += 1
                print(f"SENT | {key} | {signal['side']}")

            except Exception as error:
                logging.exception(f"{key} xatosi")
                errors.append(f"{key}: {str(error)[:150]}")
                print(f"ERROR | {key} | {error}")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if state["heartbeat_date"] != today:
        telegram_send(
            ADMIN_ID,
            "🤖 <b>Forex Signals Bot V2 faol</b>\n"
            "📊 EURUSD va XAUUSD tekshirilmoqda.\n"
            "⏱ M5 / M15 / M30 / H1 / H4"
        )
        state["heartbeat_date"] = today

    save_state(state, sha)

    print("\n========== YAKUNIY NATIJA ==========")
    print(f"Yangi signallar: {sent_count}")
    print(f"Timeframe xatolari: {len(errors)}")

    if errors:
        telegram_send(
            ADMIN_ID,
            "⚠️ <b>Timeframe xatolari:</b>\n"
            + "\n".join(errors[:10])
        )


if __name__ == "__main__":
    main()

