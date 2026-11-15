import os
import time
import requests
from datetime import datetime, timezone

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # optional; /start can register the chat
POLL_SECONDS = 60

SYMBOLS = {
    "EURUSD": "EURUSD=X",
    "XAUUSD": "XAUUSD=X",
}
INTERVAL_SECONDS = 30 * 60
ATR_PERIOD = 14
SUPERTREND_FACTOR = 2.5
SL_ATR_MULTIPLIER = 1.5
RR_TARGETS = (1.0, 2.0, 3.0)

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})
telegram_offset = 0
known_chats = set([CHAT_ID] if CHAT_ID else [])
last_signal_bar = {}


def telegram(method, payload=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = session.post(url, json=payload or {}, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data["result"]


def send_message(chat_id, text):
    telegram("sendMessage", {"chat_id": chat_id, "text": text})


def get_updates():
    global telegram_offset
    updates = telegram("getUpdates", {
        "offset": telegram_offset,
        "timeout": 10,
        "allowed_updates": ["message"]
    })
    for update in updates:
        telegram_offset = max(telegram_offset, update["update_id"] + 1)
        message = update.get("message", {})
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        text = message.get("text", "").strip()
        if chat_id and text.startswith("/start"):
            known_chats.add(chat_id)
            send_message(
                chat_id,
                "Forex Signals bot ishga tushdi. EURUSD va XAUUSD M30 signallarini "
                "yopilgan shamdan keyin kuzataman."
            )


def yahoo_bars(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    response = session.get(
        url,
        params={"range": "5d", "interval": "30m"},
        timeout=20
    )
    response.raise_for_status()
    payload = response.json()
    result = payload.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"Yahoo Finance ma'lumot bermadi: {ticker}")
    item = result[0]
    timestamps = item.get("timestamp") or []
    quote = item["indicators"]["quote"][0]
    bars = []
    now_ts = int(datetime.now(timezone.utc).timestamp())

    for i, ts in enumerate(timestamps):
        values = (quote["open"][i], quote["high"][i],
                  quote["low"][i], quote["close"][i])
        if any(v is None for v in values):
            continue
        # Faqat to'liq yopilgan 30 daqiqalik shamlar.
        if int(ts) + INTERVAL_SECONDS > now_ts:
            continue
        bars.append({
            "time": int(ts),
            "open": float(values[0]),
            "high": float(values[1]),
            "low": float(values[2]),
            "close": float(values[3]),
        })
    return bars


def rma(values, length):
    result = [None] * len(values)
    if len(values) < length:
        return result
    seed = sum(values[:length]) / length
    result[length - 1] = seed
    alpha = 1.0 / length
    prev = seed
    for i in range(length, len(values)):
        prev = alpha * values[i] + (1.0 - alpha) * prev
        result[i] = prev
    return result


def supertrend(bars):
    if len(bars) < ATR_PERIOD + 2:
        return None

    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]

    tr = []
    for i in range(len(bars)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            tr.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            ))

    atr = rma(tr, ATR_PERIOD)
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
            direction[i] = 1  # Pine direction: +1 = downtrend
            continue

        final_upper[i] = (
            basic_upper if basic_upper < final_upper[i - 1]
            or closes[i - 1] > final_upper[i - 1]
            else final_upper[i - 1]
        )
        final_lower[i] = (
            basic_lower if basic_lower > final_lower[i - 1]
            or closes[i - 1] < final_lower[i - 1]
            else final_lower[i - 1]
        )

        prev_dir = direction[i - 1]
        if prev_dir == 1:
            direction[i] = -1 if closes[i] > final_upper[i] else 1
        else:
            direction[i] = 1 if closes[i] < final_lower[i] else -1

    valid = [i for i, d in enumerate(direction) if d is not None and atr[i] is not None]
    if len(valid) < 2:
        return None
    current = valid[-1]
    previous = valid[-2]
    return {
        "index": current,
        "previous_direction": direction[previous],
        "direction": direction[current],
        "atr": atr[current],
        "close": closes[current],
        "bar_time": bars[current]["time"],
    }


def format_price(value):
    return f"{value:.5f}" if value < 10 else f"{value:.2f}"


def build_signal(symbol, result):
    is_buy = result["previous_direction"] == 1 and result["direction"] == -1
    is_sell = result["previous_direction"] == -1 and result["direction"] == 1
    if not (is_buy or is_sell):
        return None

    entry = result["close"]
    risk = result["atr"] * SL_ATR_MULTIPLIER
    if risk <= 0:
        return None

    if is_buy:
        sl = entry - risk
        tps = [entry + risk * rr for rr in RR_TARGETS]
        side = "BUY"
    else:
        sl = entry + risk
        tps = [entry - risk * rr for rr in RR_TARGETS]
        side = "SELL"

    bar_dt = datetime.fromtimestamp(result["bar_time"], timezone.utc)
    return (
        f"📊 {symbol} — {side}\n"
        f"⏱ Timeframe: M30\n"
        f"🕯 Yopilgan sham: {bar_dt:%Y-%m-%d %H:%M} UTC\n\n"
        f"Entry: {format_price(entry)}\n"
        f"Stop Loss: {format_price(sl)}\n"
        f"TP1: {format_price(tps[0])}\n"
        f"TP2: {format_price(tps[1])}\n"
        f"TP3: {format_price(tps[2])}\n\n"
        "Hisob: Supertrend (ATR 14, Factor 2.5), SL = ATR × 1.5.\n"
        "Ma'lumot Yahoo Finance'dan; broker narxidan farq qilishi mumkin. "
        "Bu avtomatik savdo buyrug'i emas."
    )


def scan_markets():
    for symbol, ticker in SYMBOLS.items():
        try:
            bars = yahoo_bars(ticker)
            result = supertrend(bars)
            if not result:
                continue
            signal = build_signal(symbol, result)
            if not signal:
                continue
            bar_time = result["bar_time"]
            if last_signal_bar.get(symbol) == bar_time:
                continue
            last_signal_bar[symbol] = bar_time
            for chat_id in list(known_chats):
                send_message(chat_id, signal)
            print(f"Signal yuborildi: {symbol} {bar_time}")
        except Exception as exc:
            print(f"{symbol} xatolik: {exc}")


def main():
    print("Bot ishga tushdi.")
    while True:
        try:
            get_updates()
            scan_markets()
        except Exception as exc:
            print(f"Asosiy sikl xatoligi: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
