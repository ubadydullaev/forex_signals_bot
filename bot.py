 
import json
import os
from datetime import datetime, timezone

import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOLS = {
    "EURUSD": "https://biquote.io/api/EURUSD/ohlc",
    "XAUUSD": "https://biquote.io/api/XAUUSD/ohlc",
}

ATR_PERIOD = 14
SUPERTREND_FACTOR = 2.5
SL_ATR_MULTIPLIER = 1.5
RR_TARGETS = (1.0, 2.0, 3.0)
STATE_FILE = ".signal_state.json"

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})


def telegram(method, payload=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = session.post(url, json=payload or {}, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data["result"]


def send_message(text):
    telegram("sendMessage", {"chat_id": CHAT_ID, "text": text})


def biquote_bars(url):
    response = session.get(
        url,
        params={"interval": "30m", "limit": 200},
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()
    raw_bars = payload.get("bars", [])

    if not raw_bars:
        raise RuntimeError("BiQuote sham ma'lumotini bermadi.")

    bars = []

    for item in raw_bars:
        # Hozir shakllanayotgan shamni hisobga olmaymiz.
        if item.get("isOpen") is not False:
            continue

        values = (
            item.get("open"),
            item.get("high"),
            item.get("low"),
            item.get("close"),
        )

        if any(value is None for value in values):
            continue

        bar_dt = datetime.fromisoformat(
            item["openTime"].replace("Z", "+00:00")
        )

        bars.append({
            "time": int(bar_dt.timestamp()),
            "open": float(values[0]),
            "high": float(values[1]),
            "low": float(values[2]),
            "close": float(values[3]),
        })

    # API yangi shamlarni birinchi qaytaradi.
    # Hisoblash uchun eski shamlar oldinda bo'lishi kerak.
    bars.sort(key=lambda bar: bar["time"])

    if len(bars) < ATR_PERIOD + 2:
        raise RuntimeError(
            f"Yopilgan shamlar yetarli emas: {len(bars)}"
        )

    return bars


def rma(values, length):
    result = [None] * len(values)

    if len(values) < length:
        return result

    seed = sum(values[:length]) / length
    result[length - 1] = seed
    alpha = 1.0 / length
    previous = seed

    for i in range(length, len(values)):
        previous = (
            alpha * values[i]
            + (1.0 - alpha) * previous
        )
        result[i] = previous

    return result


def supertrend(bars):
    if len(bars) < ATR_PERIOD + 2:
        return None

    highs = [bar["high"] for bar in bars]
    lows = [bar["low"] for bar in bars]
    closes = [bar["close"] for bar in bars]

    true_ranges = []

    for i in range(len(bars)):
        if i == 0:
            true_ranges.append(highs[i] - lows[i])
        else:
            true_ranges.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))

    atr = rma(true_ranges, ATR_PERIOD)
    final_upper = [None] * len(bars)
    final_lower = [None] * len(bars)
    direction = [None] * len(bars)

    for i in range(len(bars)):
        if atr[i] is None:
            continue

        midpoint = (highs[i] + lows[i]) / 2.0
        basic_upper = midpoint + SUPERTREND_FACTOR * atr[i]
        basic_lower = midpoint - SUPERTREND_FACTOR * atr[i]

        if i == 0 or final_upper[i - 1] is None:
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower
            direction[i] = 1
            continue

        final_upper[i] = (
            basic_upper
            if basic_upper < final_upper[i - 1]
            or closes[i - 1] > final_upper[i - 1]
            else final_upper[i - 1]
        )

        final_lower[i] = (
            basic_lower
            if basic_lower > final_lower[i - 1]
            or closes[i - 1] < final_lower[i - 1]
            else final_lower[i - 1]
        )

        previous_direction = direction[i - 1]

        if previous_direction == 1:
            direction[i] = (
                -1 if closes[i] > final_upper[i] else 1
            )
        else:
            direction[i] = (
                1 if closes[i] < final_lower[i] else -1
            )

    valid = [
        i for i, value in enumerate(direction)
        if value is not None and atr[i] is not None
    ]

    if len(valid) < 2:
        return None

    current = valid[-1]
    previous = valid[-2]

    return {
        "previous_direction": direction[previous],
        "direction": direction[current],
        "atr": atr[current],
        "close": closes[current],
        "bar_time": bars[current]["time"],
    }


def format_price(value):
    return f"{value:.5f}" if value < 10 else f"{value:.2f}"


def build_signal(symbol, result):
    is_buy = (
        result["previous_direction"] == 1
        and result["direction"] == -1
    )

    is_sell = (
        result["previous_direction"] == -1
        and result["direction"] == 1
    )

    if not (is_buy or is_sell):
        return None

    entry = result["close"]
    risk = result["atr"] * SL_ATR_MULTIPLIER

    if risk <= 0:
        return None

    if is_buy:
        stop_loss = entry - risk
        targets = [
            entry + risk * rr for rr in RR_TARGETS
        ]
        side = "BUY"
    else:
        stop_loss = entry + risk
        targets = [
            entry - risk * rr for rr in RR_TARGETS
        ]
        side = "SELL"

    bar_dt = datetime.fromtimestamp(
        result["bar_time"], timezone.utc
    )

    return (
        f"📊 {symbol} — {side}\n"
        "⏱ Timeframe: M30\n"
        f"🕯 Yopilgan sham: {bar_dt:%Y-%m-%d %H:%M} UTC\n\n"
        f"Entry: {format_price(entry)}\n"
        f"Stop Loss: {format_price(stop_loss)}\n"
        f"TP1: {format_price(targets[0])}\n"
        f"TP2: {format_price(targets[1])}\n"
        f"TP3: {format_price(targets[2])}\n\n"
        "Hisob: Supertrend (ATR 14, Factor 2.5), "
        "SL = ATR × 1.5.\n"
        "Ma'lumot manbasi: BiQuote (Broker 1). "
        "ForexClub MT5 narxidan farq qilishi mumkin. "
        "Bu avtomatik savdo buyrug'i emas."
    )


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file)


def main():
    state = load_state()
    changed = False
    today = datetime.now(timezone.utc).date().isoformat()

    # Kuniga bir marta bot ishlayotganini bildiradi.
    if state.get("_heartbeat_date") != today:
        send_message(
            "🟢 Bot ishlayapti! EURUSD va XAUUSD tekshirilmoqda."
        )
        state["_heartbeat_date"] = today
        changed = True

    errors = state.setdefault("_errors", {})

    for symbol, url in SYMBOLS.items():
        try:
            bars = biquote_bars(url)
            result = supertrend(bars)

            if not result:
                print(f"{symbol}: yetarli sham ma'lumoti yo'q.")
                continue

            if errors.get(symbol):
                send_message(f"🟢 {symbol} qayta ishlayapti!")
                errors.pop(symbol, None)
                changed = True

            signal = build_signal(symbol, result)

            if not signal:
                print(f"{symbol}: yangi signal yo'q.")
                continue

            bar_time = str(result["bar_time"])

            if state.get(symbol) == bar_time:
                print(f"{symbol}: bu signal avval yuborilgan.")
                continue

            send_message(signal)
            state[symbol] = bar_time
            changed = True
            print(f"{symbol}: signal Telegramga yuborildi.")

        except Exception as exc:
            print(f"{symbol} xatolik: {exc}")

            if not errors.get(symbol):
                try:
                    send_message(
                        f"🔴 {symbol} ishlamayapti!\n"
                        f"Xatolik: {exc}"
                    )
                    errors[symbol] = True
                    changed = True
                except Exception as notify_exc:
                    print(
                        f"Xabar yuborishda xatolik: {notify_exc}"
                    )

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()



