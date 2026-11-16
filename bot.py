
import os
import json
import base64
import requests
from datetime import datetime, timezone

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ["TELEGRAM_CHAT_ID"])
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

STATE_FILE = "bot_state.json"

SYMBOLS = {
    "EURUSD": "https://biquote.io/api/EURUSD/ohlc",
    "XAUUSD": "https://biquote.io/api/XAUUSD/ohlc",
}

ATR_PERIOD = 14
SUPERTREND_FACTOR = 2.5
SL_ATR_MULTIPLIER = 1.5
RR_TARGETS = (1.0, 2.0, 3.0)


def telegram(method, payload=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, json=payload or {}, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data)
    return data["result"]


def send_message(chat_id, message):
    telegram("sendMessage", {
        "chat_id": chat_id,
        "text": message
    })


def load_state():
    url = f"https://api.github.com/repos/{REPO}/contents/{STATE_FILE}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    response = requests.get(
        url, headers=headers, params={"ref": BRANCH}, timeout=30
    )

    if response.status_code == 404:
        return {
            "pending": [],
            "users": [],
            "update_offset": 0,
            "last_signals": {},
            "heartbeat_date": ""
        }

    response.raise_for_status()
    data = response.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    state = json.loads(content)
    state["_sha"] = data["sha"]
    return state


def save_state(state):
    url = f"https://api.github.com/repos/{REPO}/contents/{STATE_FILE}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    clean_state = {
        key: value for key, value in state.items()
        if key != "_sha"
    }

    content = base64.b64encode(
        json.dumps(clean_state, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")

    payload = {
        "message": "Update bot users and signal state",
        "content": content,
        "branch": BRANCH
    }

    if state.get("_sha"):
        payload["sha"] = state["_sha"]

    response = requests.put(
        url, headers=headers, json=payload, timeout=30
    )
    response.raise_for_status()
    state["_sha"] = response.json()["content"]["sha"]


def get_updates(state):
    payload = {"timeout": 0, "limit": 100}
    if state.get("update_offset"):
        payload["offset"] = state["update_offset"]

    updates = telegram("getUpdates", payload)

    for update in updates:
        state["update_offset"] = update["update_id"] + 1

    return updates


def process_updates(state, updates):
    pending = set(state.get("pending", []))
    users = set(state.get("users", []))

    for update in updates:
        message = update.get("message", {})
        if not message:
            continue

        chat_id = message.get("chat", {}).get("id")
        user_id = message.get("from", {}).get("id")
        username = message.get("from", {}).get("username", "")
        command = message.get("text", "").strip()

        if not chat_id or not user_id or not command:
            continue

        if user_id == ADMIN_ID:
            parts = command.split()
            action = parts[0].lower()

            if action == "/users":
                text = "Tasdiqlangan foydalanuvchilar:\n"
                text += "\n".join(map(str, sorted(users))) or "Hozircha yo‘q"
                text += "\n\nKutilayotganlar:\n"
                text += "\n".join(map(str, sorted(pending))) or "Yo‘q"
                send_message(ADMIN_ID, text)

            elif action == "/approve" and len(parts) == 2:
                target = int(parts[1])
                pending.discard(target)
                users.add(target)
                send_message(ADMIN_ID, f"✅ {target} tasdiqlandi.")
                try:
                    send_message(target, "✅ Ruxsatingiz tasdiqlandi! Endi signallarni olasiz.")
                except Exception:
                    pass

            elif action == "/reject" and len(parts) == 2:
                target = int(parts[1])
                pending.discard(target)
                send_message(ADMIN_ID, f"❌ {target} rad etildi.")
                try:
                    send_message(target, "❌ Ruxsat so‘rovingiz rad etildi.")
                except Exception:
                    pass

            elif action == "/revoke" and len(parts) == 2:
                target = int(parts[1])
                users.discard(target)
                send_message(ADMIN_ID, f"⛔ {target} ruxsati bekor qilindi.")
                try:
                    send_message(target, "⛔ Botdan foydalanish ruxsatingiz bekor qilindi.")
                except Exception:
                    pass

            else:
                send_message(
                    ADMIN_ID,
                    "Buyruqlar:\n/users\n/approve ID\n/reject ID\n/revoke ID"
                )

        elif command == "/start":
            if user_id in users:
                send_message(chat_id, "✅ Siz tasdiqlangansiz. Signallarni kuting.")
            elif user_id in pending:
                send_message(chat_id, "⏳ So‘rovingiz admin ko‘rib chiqishida.")
            else:
                pending.add(user_id)
                send_message(chat_id, "⏳ Ruxsat so‘rovingiz yuborildi.")
                send_message(
                    ADMIN_ID,
                    f"🔔 Yangi ruxsat so‘rovi!\nID: {user_id}\nUsername: @{username or 'yo‘q'}\n\nTasdiqlash: /approve {user_id}"
                )

    state["pending"] = sorted(pending)
    state["users"] = sorted(users)


def get_bars(url):
    response = requests.get(
        url,
        params={"interval": "30m", "limit": 200},
        timeout=30
    )
    response.raise_for_status()

    bars = response.json()["bars"]
    bars = [b for b in bars if b.get("isOpen") is False]
    bars.sort(key=lambda b: b["openTime"])

    return bars


def calculate_signal(bars):
    if len(bars) < 50:
        return None

    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    closes = [float(b["close"]) for b in bars]

    true_ranges = []
    for i in range(len(bars)):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
        true_ranges.append(tr)

    atr = [None] * len(bars)
    atr[ATR_PERIOD - 1] = sum(true_ranges[:ATR_PERIOD]) / ATR_PERIOD

    for i in range(ATR_PERIOD, len(bars)):
        atr[i] = (
            atr[i - 1] * (ATR_PERIOD - 1) + true_ranges[i]
        ) / ATR_PERIOD

    upper = [None] * len(bars)
    lower = [None] * len(bars)
    direction = [None] * len(bars)

    for i in range(ATR_PERIOD - 1, len(bars)):
        mid = (highs[i] + lows[i]) / 2
        basic_upper = mid + SUPERTREND_FACTOR * atr[i]
        basic_lower = mid - SUPERTREND_FACTOR * atr[i]

        if i == ATR_PERIOD - 1:
            upper[i] = basic_upper
            lower[i] = basic_lower
            direction[i] = 1
            continue

        upper[i] = (
            basic_upper
            if basic_upper < upper[i - 1] or closes[i - 1] > upper[i - 1]
            else upper[i - 1]
        )
        lower[i] = (
            basic_lower
            if basic_lower > lower[i - 1] or closes[i - 1] < lower[i - 1]
            else lower[i - 1]
        )

        if direction[i - 1] == 1:
            direction[i] = -1 if closes[i] > upper[i] else 1
        else:
            direction[i] = 1 if closes[i] < lower[i] else -1

    valid = [
        i for i, value in enumerate(direction)
        if value is not None
    ]

    previous = valid[-2]
    current = valid[-1]

    if direction[previous] == direction[current]:
        return None

    side = "BUY" if direction[current] == -1 else "SELL"
    entry = closes[current]
    distance = atr[current] * SL_ATR_MULTIPLIER

    if side == "BUY":
        sl = entry - distance
        tps = [entry + distance * rr for rr in RR_TARGETS]
    else:
        sl = entry + distance
        tps = [entry - distance * rr for rr in RR_TARGETS]

    return {
        "side": side,
        "entry": entry,
        "sl": sl,
        "tps": tps,
        "time": bars[current]["openTime"]
    }


def format_signal(symbol, signal):
    return (
        f"📊 {symbol} — {signal['side']}\n"
        f"⏱ Timeframe: M30\n"
        f"🕯 Yopilgan sham: {signal['time']}\n\n"
        f"Entry: {signal['entry']:.5f}\n"
        f"Stop Loss: {signal['sl']:.5f}\n"
        f"TP1: {signal['tps'][0]:.5f}\n"
        f"TP2: {signal['tps'][1]:.5f}\n"
        f"TP3: {signal['tps'][2]:.5f}\n\n"
        "Hisob: Supertrend (ATR 14, Factor 2.5), "
        "SL = ATR × 1.5.\n"
        "Manba: BiQuote (Broker 1). ForexClub bilan "
        "narxlar farq qilishi mumkin. Demo hisobda tekshiring."
    )


def main():
    state = load_state()

    try:
        updates = get_updates(state)
        process_updates(state, updates)

        for symbol, url in SYMBOLS.items():
            bars = get_bars(url)
            signal = calculate_signal(bars)

            if not signal:
                print(f"{symbol}: yangi signal yo‘q")
                continue

            if state.get("last_signals", {}).get(symbol) == signal["time"]:
                print(f"{symbol}: bu sham signali oldin yuborilgan")
                continue

            message = format_signal(symbol, signal)
            recipients = set(state.get("users", []))
            recipients.add(ADMIN_ID)

            for recipient in recipients:
                try:
                    send_message(recipient, message)
                except Exception as error:
                    print(f"Xabar yuborilmadi {recipient}: {error}")

            state.setdefault("last_signals", {})[symbol] = signal["time"]
            print(f"{symbol}: {signal['side']} signali yuborildi")

        today = datetime.now(timezone.utc).date().isoformat()
        if state.get("heartbeat_date") != today:
            send_message(
                ADMIN_ID,
                "🟢 Bot ishlayapti! EURUSD va XAUUSD tekshirilmoqda."
            )
            state["heartbeat_date"] = today

    finally:
        save_state(state)


if __name__ == "__main__":
    main()

                
