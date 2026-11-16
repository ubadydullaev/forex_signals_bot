
import os
import json
import base64
import requests
from datetime import datetime, timezone

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = str(os.environ["TELEGRAM_CHAT_ID"])
WORKER_URL = os.environ["CLOUDFLARE_WORKER_URL"].rstrip("/")
WORKER_SECRET = os.environ["WEBHOOK_SECRET"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
GITHUB_BRANCH = os.getenv("GITHUB_REF_NAME", "main")

SYMBOLS = {
    "EURUSD": "https://biquote.io/api/EURUSD/ohlc?interval=30m&limit=200",
    "XAUUSD": "https://biquote.io/api/XAUUSD/ohlc?interval=30m&limit=200",
}

ATR_PERIOD = 14
SUPERTREND_FACTOR = 2.5
SL_ATR_MULTIPLIER = 1.5
RR_TARGETS = (1.0, 2.0, 3.0)

STATE_FILE = "bot_state.json"
session = requests.Session()


def telegram_send(chat_id, message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = session.post(
        url,
        json={"chat_id": chat_id, "text": message},
        timeout=20,
    )
    response.raise_for_status()


def load_state():
    url = (
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
        f"/contents/{STATE_FILE}"
    )
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    response = session.get(
        url,
        headers=headers,
        params={"ref": GITHUB_BRANCH},
        timeout=20,
    )

    if response.status_code == 404:
        return {"last_signals": {}, "heartbeat_date": ""}, None

    response.raise_for_status()
    data = response.json()
    content = base64.b64decode(data["content"]).decode("utf-8")

    return json.loads(content), data["sha"]


def save_state(state, sha):
    url = (
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
        f"/contents/{STATE_FILE}"
    )
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    content = base64.b64encode(
        json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")

    payload = {
        "message": "Update signal state",
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


def approved_users():
    url = f"{WORKER_URL}/approved-users"
    response = session.get(
        url,
        headers={"X-Worker-Secret": WORKER_SECRET},
        timeout=20,
    )
    response.raise_for_status()

    data = response.json()
    users = [str(x) for x in data.get("users", [])]

    return sorted(set(users + [ADMIN_ID]))

def get_candles(url):
    response = session.get(url, timeout=25)
    response.raise_for_status()
    data = response.json()
    print("API JAVOBI:", str(data)[:3000])

    if isinstance(data, dict):
        data = data.get("bars", data.get("data", data.get("candles", data.get("result", []))))
    if not isinstance(data, list):
        raise ValueError("OHLC ma'lumotlari ro'yxat emas")

    candles = []

    for item in data:
        if not isinstance(item, dict):
            continue

        if item.get("isOpen") is not True:
            continue

        try:
            candle = {
                "time": str(item.get("openTime", item.get("timestamp", item.get("time", "")))),
                "open": float(item.get("open", item.get("o"))),
                "high": float(item.get("high", item.get("h"))),
                "low": float(item.get("low", item.get("l"))),
                "close": float(item.get("close", item.get("c"))),
            }
            if candle["time"]:
                candles.append(candle)
        except (TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["time"])

    if len(candles) < ATR_PERIOD + 3:
        raise ValueError("Yopilgan shamlar yetarli emas")

    return candles


def calculate_atr(candles):
    trs = []

    for i, candle in enumerate(candles):
        if i == 0:
            tr = candle["high"] - candle["low"]
        else:
            previous_close = candles[i - 1]["close"]
            tr = max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close),
            )
        trs.append(tr)

    atr = [None] * len(candles)
    period = ATR_PERIOD

    atr[period - 1] = sum(trs[:period]) / period

    for i in range(period, len(candles)):
        atr[i] = (
            atr[i - 1] * (period - 1) + trs[i]
        ) / period

    return atr


def calculate_supertrend(candles):
    atr = calculate_atr(candles)
    upper = [None] * len(candles)
    lower = [None] * len(candles)
    direction = [0] * len(candles)

    for i, candle in enumerate(candles):
        if atr[i] is None:
            continue

        midpoint = (candle["high"] + candle["low"]) / 2
        basic_upper = midpoint + SUPERTREND_FACTOR * atr[i]
        basic_lower = midpoint - SUPERTREND_FACTOR * atr[i]

        if i == ATR_PERIOD - 1:
            upper[i] = basic_upper
            lower[i] = basic_lower
            direction[i] = 1
            continue

        previous_upper = upper[i - 1]
        previous_lower = lower[i - 1]
        previous_close = candles[i - 1]["close"]

        upper[i] = (
            basic_upper
            if basic_upper < previous_upper or previous_close > previous_upper
            else previous_upper
        )

        lower[i] = (
            basic_lower
            if basic_lower > previous_lower or previous_close < previous_lower
            else previous_lower
        )

        if direction[i - 1] == 1:
            direction[i] = -1 if candle["close"] > upper[i] else 1
        else:
            direction[i] = 1 if candle["close"] < lower[i] else -1

    return direction, atr


def build_signal(symbol, candles):
    direction, atr = calculate_supertrend(candles)

    i = len(candles) - 1
    previous = i - 1

    if atr[i] is None:
        return None

    if direction[i] == direction[previous]:
        return None

    side = "BUY" if direction[i] == -1 else "SELL"
    entry = candles[i]["close"]
    risk = atr[i] * SL_ATR_MULTIPLIER

    if side == "BUY":
        sl = entry - risk
        tps = [entry + risk * rr for rr in RR_TARGETS]
    else:
        sl = entry + risk
        tps = [entry - risk * rr for rr in RR_TARGETS]

    return {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tps": tps,
        "time": candles[i]["time"],
    }


def format_signal(signal):
    digits = 3 if signal["symbol"] == "XAUUSD" else 5

    return (
        f"🚨 {signal['symbol']} M30 — {signal['side']}\n\n"
        f"📍 Entry: {signal['entry']:.{digits}f}\n"
        f"🛑 SL: {signal['sl']:.{digits}f}\n\n"
        f"🎯 TP1: {signal['tps'][0]:.{digits}f}\n"
        f"🎯 TP2: {signal['tps'][1]:.{digits}f}\n"
        f"🎯 TP3: {signal['tps'][2]:.{digits}f}\n\n"
        f"⏱ Time: {signal['time']}\n"
        f"⚠️ Demo hisobda tekshiring. Signal kafolatlangan emas."
    )


def main():
    state, sha = load_state()
    users = approved_users()

    for symbol, url in SYMBOLS.items():
        candles = get_candles(url)
        signal = build_signal(symbol, candles)

        if signal is None:
            continue

        signal_key = f"{symbol}_{signal['time']}"

        if state["last_signals"].get(symbol) == signal_key:
            continue

        message = format_signal(signal)

        for user_id in users:
            try:
                telegram_send(user_id, message)
            except Exception as error:
                print(f"Telegram yuborish xatosi {user_id}: {error}")

        state["last_signals"][symbol] = signal_key

    today = datetime.now(timezone.utc).date().isoformat()

    if state.get("heartbeat_date") != today:
        telegram_send(
            ADMIN_ID,
            "🟢 Bot ishlayapti! EURUSD va XAUUSD tekshirilmoqda."
        )
        state["heartbeat_date"] = today

    save_state(state, sha)


if __name__ == "__main__":
    main()
