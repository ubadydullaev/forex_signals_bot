
import os
import json
import time
import base64
import logging
from datetime import datetime, timezone

import requests

# =============== CONFIG ===============

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = str(os.environ["TELEGRAM_CHAT_ID"])
WORKER_URL = os.environ["CLOUDFLARE_WORKER_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPOSITORY = os.environ["GITHUB_REPOSITORY"]
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

API_URL = "https://biquote.io/api/{symbol}/ohlc"
STATE_FILE = "bot_v4_state.json"

SYMBOLS = ["XAUUSD", "EURUSD"]
TIMEFRAMES = {
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
}

ATR_PERIOD = 14
MIN_SCORE = 70
SL_ATR = 1.5
RR = (1.0, 2.0, 3.0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update({"User-Agent": "ForexSignalsBot/4.0"})

# =============== MARKET DATA ===============

def time_key(value):
    value = str(value)
    try:
        return (0, float(value))
    except ValueError:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (1, dt.timestamp())
        except ValueError:
            return (2, value)


def get_candles(symbol, interval):
    response = session.get(
        API_URL.format(symbol=symbol),
        params={"interval": interval, "limit": 300},
        timeout=25
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
        raise ValueError("Market API candle ro'yxatini qaytarmadi")

    result = []

    for item in bars:
        if not isinstance(item, dict):
            continue
        if item.get("isOpen") is True:
            continue

        try:
            stamp = item.get(
                "openTime",
                item.get("timestamp", item.get("time", ""))
            )
            result.append({
                "time": str(stamp),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            })
        except (KeyError, TypeError, ValueError):
            continue

    result.sort(key=lambda x: time_key(x["time"]))

    if len(result) < 60:
        raise ValueError(
            f"{symbol} {interval}: shamlar kam ({len(result)})"
        )

    return result


# =============== INDICATORS ===============

def ema_series(values, period):
    output = [None] * len(values)
    if len(values) < period:
        return output

    current = sum(values[:period]) / period
    output[period - 1] = current
    multiplier = 2 / (period + 1)

    for i in range(period, len(values)):
        current = (
            values[i] * multiplier
            + current * (1 - multiplier)
        )
        output[i] = current

    return output


def rsi_series(values, period=14):
    output = [None] * len(values)
    if len(values) <= period:
        return output

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def calc(g, l):
        if l == 0:
            return 100.0
        rs = g / l
        return 100 - (100 / (1 + rs))

    output[period] = calc(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i - 1]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[i - 1]
        ) / period
        output[i] = calc(avg_gain, avg_loss)

    return output


def atr_series(candles, period=14):
    tr = []
    output = [None] * len(candles)

    for i, c in enumerate(candles):
        if i == 0:
            value = c["high"] - c["low"]
        else:
            previous = candles[i - 1]["close"]
            value = max(
                c["high"] - c["low"],
                abs(c["high"] - previous),
                abs(c["low"] - previous)
            )
        tr.append(value)

    if len(tr) < period:
        return output

    current = sum(tr[:period]) / period
    output[period - 1] = current

    for i in range(period, len(tr)):
        current = (
            current * (period - 1) + tr[i]
        ) / period
        output[i] = current

    return output


def adx_value(candles, period=14):
    if len(candles) < period * 2 + 2:
        return None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        up = current["high"] - previous["high"]
        down = previous["low"] - current["low"]

        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)

        trs.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        ))

    dx_values = []

    for i in range(period, len(trs) + 1):
        tr_sum = sum(trs[i - period:i])
        if tr_sum <= 0:
            continue

        plus = sum(plus_dm[i - period:i]) / tr_sum * 100
        minus = sum(minus_dm[i - period:i]) / tr_sum * 100

        total = plus + minus
        dx = abs(plus - minus) / total * 100 if total else 0
        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    return sum(dx_values[-period:]) / period


def supertrend_direction(candles, atr_values, factor=2.5):
    upper = None
    lower = None
    direction = 1

    for i, candle in enumerate(candles):
        atr = atr_values[i]
        if atr is None:
            continue

        middle = (candle["high"] + candle["low"]) / 2
        basic_upper = middle + factor * atr
        basic_lower = middle - factor * atr

        if upper is None:
            upper = basic_upper
            lower = basic_lower
            continue

        previous_close = candles[i - 1]["close"]

        upper = (
            basic_upper
            if basic_upper < upper or previous_close > upper
            else upper
        )
        lower = (
            basic_lower
            if basic_lower > lower or previous_close < lower
            else lower
        )

        if direction == 1 and candle["close"] < lower:
            direction = -1
        elif direction == -1 and candle["close"] > upper:
            direction = 1

    return direction


# =============== MARKET STRUCTURE ===============

def structure_checks(candles):
    current = candles[-1]
    previous = candles[-2]
    recent = candles[-21:-1]

    if len(recent) < 10:
        return False, False, False

    old_high = max(x["high"] for x in recent)
    old_low = min(x["low"] for x in recent)

    # Liquidity sweep: wick passes level, close returns inside
    buy_sweep = (
        current["low"] < old_low
        and current["close"] > old_low
    )
    sell_sweep = (
        current["high"] > old_high
        and current["close"] < old_high
    )

    # Break of recent structure
    buy_bos = current["close"] > old_high
    sell_bos = current["close"] < old_low

    # Simple three-candle fair value gap
    buy_fvg = (
        len(candles) >= 3
        and candles[-1]["low"] > candles[-3]["high"]
    )
    sell_fvg = (
        len(candles) >= 3
        and candles[-1]["high"] < candles[-3]["low"]
    )

    return (
        buy_sweep or sell_sweep,
        buy_bos or sell_bos,
        buy_fvg or sell_fvg
    )


# =============== TREND ANALYSIS ===============

def trend_direction(candles):
    closes = [x["close"] for x in candles]
    e20 = ema_series(closes, 20)[-1]
    e50 = ema_series(closes, 50)[-1]

    if e20 is None or e50 is None:
        return 0

    if e20 > e50 and closes[-1] > e20:
        return 1
    if e20 < e50 and closes[-1] < e20:
        return -1

    return 0


# =============== SIGNAL ENGINE ===============

def analyze(symbol, timeframe, candles, h1, h4):
    closes = [x["close"] for x in candles]
    current = candles[-1]

    ema20 = ema_series(closes, 20)[-1]
    ema50 = ema_series(closes, 50)[-1]
    ema200 = ema_series(closes, 200)[-1]
    rsi = rsi_series(closes)[-1]
    atr_values = atr_series(candles, ATR_PERIOD)
    atr = atr_values[-1]
    adx = adx_value(candles)

    if any(x is None for x in (ema20, ema50, rsi, atr)):
        return None

    if atr <= 0:
        return None

    st = supertrend_direction(candles, atr_values)
    h1_direction = trend_direction(h1)
    h4_direction = trend_direction(h4)

    sweep, bos, fvg = structure_checks(candles)

    buy_score = 0
    sell_score = 0

    # EMA trend
    if ema20 > ema50:
        buy_score += 15
    elif ema20 < ema50:
        sell_score += 15

    # EMA200, when enough candles are available
    if ema200 is not None:
        if current["close"] > ema200:
            buy_score += 10
        elif current["close"] < ema200:
            sell_score += 10

    # Supertrend
    if st == 1:
        buy_score += 15
    else:
        sell_score += 15

    # RSI momentum
    if 50 < rsi < 72:
        buy_score += 10
    elif 28 < rsi < 50:
        sell_score += 10

    # ADX trend strength
    if adx is not None and adx >= 18:
        if ema20 > ema50:
            buy_score += 10
        elif ema20 < ema50:
            sell_score += 10

    # Higher timeframe agreement
    if h1_direction == 1:
        buy_score += 8
    elif h1_direction == -1:
        sell_score += 8

    if h4_direction == 1:
        buy_score += 8
    elif h4_direction == -1:
        sell_score += 8

    # Price location
    if current["close"] > ema20:
        buy_score += 5
    elif current["close"] < ema20:
        sell_score += 5

    # Structure confluence
    if sweep:
        if current["close"] > current["open"]:
            buy_score += 8
        elif current["close"] < current["open"]:
            sell_score += 8

    if bos:
        if current["close"] > current["open"]:
            buy_score += 6
        elif current["close"] < current["open"]:
            sell_score += 6

    if fvg:
        if current["close"] > current["open"]:
            buy_score += 5
        elif current["close"] < current["open"]:
            sell_score += 5

    # Select side
    if buy_score >= MIN_SCORE and buy_score > sell_score:
        side = "BUY"
        score = buy_score
    elif sell_score >= MIN_SCORE and sell_score > buy_score:
        side = "SELL"
        score = sell_score
    else:
        return None

    entry = current["close"]
    risk = atr * SL_ATR

    if side == "BUY":
        sl = entry - risk
        targets = [entry + risk * x for x in RR]
    else:
        sl = entry + risk
        targets = [entry - risk * x for x in RR]

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "score": score,
        "entry": entry,
        "sl": sl,
        "tp1": targets[0],
        "tp2": targets[1],
        "tp3": targets[2],
        "candle_time": current["time"],
    }


# =============== TELEGRAM USERS ===============

def get_users():
    response = session.get(
        f"{WORKER_URL}/approved-users",
        headers={"X-Worker-Secret": WEBHOOK_SECRET},
        timeout=20
    )
    response.raise_for_status()

    users = response.json().get("users", [])
    return sorted(set([ADMIN_ID] + [str(x) for x in users]))


def send_telegram(chat_id, message):
    response = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": str(chat_id),
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(str(result))

    return True


def price_text(symbol, value):
    digits = 3 if symbol == "XAUUSD" else 5
    return f"{value:.{digits}f}"


def format_signal(signal):
    symbol = signal["symbol"]
    emoji = "🟢" if signal["side"] == "BUY" else "🔴"

    return (
        f"{emoji} <b>{symbol} — {signal['side']}</b>\n"
        f"⏱ Timeframe: <b>{signal['timeframe']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🎯 Entry: <code>{price_text(symbol, signal['entry'])}</code>\n"
        f"🛑 SL: <code>{price_text(symbol, signal['sl'])}</code>\n"
        f"✅ TP1: <code>{price_text(symbol, signal['tp1'])}</code>\n"
        f"✅ TP2: <code>{price_text(symbol, signal['tp2'])}</code>\n"
        f"✅ TP3: <code>{price_text(symbol, signal['tp3'])}</code>\n"
        f"━━━━━━━━━━━━━━\n"
        f"📊 Confluence score: <b>{signal['score']}</b>\n"
        f"🧠 EMA + RSI + ADX + Supertrend\n"
        f"📐 Structure + liquidity + FVG\n"
        f"🕯 Yopilgan sham asosida\n"
        f"⚠️ Bu avtomatik savdo emas. Avval demo hisobda tekshiring."
    )


# =============== GITHUB STATE ===============

def github_state_url():
    return (
        f"https://api.github.com/repos/{REPOSITORY}"
        f"/contents/{STATE_FILE}"
    )


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_state():
    response = session.get(
        github_state_url(),
        headers=github_headers(),
        params={"ref": BRANCH},
        timeout=20
    )

    if response.status_code == 404:
        return {"signals": {}}, None

    response.raise_for_status()
    data = response.json()

    content = base64.b64decode(data["content"]).decode("utf-8")
    state = json.loads(content)
    state.setdefault("signals", {})

    return state, data["sha"]


def save_state(state, sha):
    content = json.dumps(
        state,
        ensure_ascii=False,
        indent=2
    ).encode("utf-8")

    payload = {
        "message": "Update V4 forex signal state",
        "content": base64.b64encode(content).decode("utf-8"),
        "branch": BRANCH,
    }

    if sha:
        payload["sha"] = sha

    response = session.put(
        github_state_url(),
        headers=github_headers(),
        json=payload,
        timeout=25
    )
    response.raise_for_status()


# =============== MAIN ===============

def main():
    logging.info("Forex V4 ishga tushdi")

    state, state_sha = load_state()
    users = get_users()

    logging.info("Qabul qiluvchilar soni: %s", len(users))

    candle_cache = {}
    new_signals = []

    # Fetch each symbol/timeframe once
    for symbol in SYMBOLS:
        for timeframe, interval in TIMEFRAMES.items():
            try:
                candle_cache[(symbol, timeframe)] = get_candles(
                    symbol, interval
                )
                logging.info(
                    "%s %s: %s candles",
                    symbol,
                    timeframe,
                    len(candle_cache[(symbol, timeframe)])
                )
            except Exception:
                logging.exception("%s %s data xatosi", symbol, timeframe)

    for symbol in SYMBOLS:
        h1 = candle_cache.get((symbol, "H1"))
        h4 = candle_cache.get((symbol, "H4"))

        if not h1 or not h4:
            logging.warning("%s: H1/H4 ma'lumoti yo'q", symbol)
            continue

        for timeframe in TIMEFRAMES:
            candles = candle_cache.get((symbol, timeframe))
            if not candles:
                continue

            try:
                signal = analyze(
                    symbol, timeframe, candles, h1, h4
                )

                if signal is None:
                    continue

                key = f"{symbol}_{timeframe}"
                signature = (
                    f"{signal['candle_time']}_{signal['side']}"
                )

                if state["signals"].get(key) == signature:
                    logging.info("%s: takroriy signal o'tkazildi", key)
                    continue

                new_signals.append((key, signature, signal))

            except Exception:
                logging.exception("%s %s analiz xatosi", symbol, timeframe)

    sent_signals = 0

    for key, signature, signal in new_signals:
        message = format_signal(signal)
        successful = 0

        for user_id in users:
            try:
                send_telegram(user_id, message)
                successful += 1
            except Exception:
                logging.exception("Telegram yuborilmadi: %s", user_id)

            time.sleep(0.15)

        if successful > 0:
            state["signals"][key] = signature
            sent_signals += 1
            logging.info(
                "%s %s yuborildi. Qabul qilganlar: %s",
                key, signal["side"], successful
            )
        else:
            logging.error("%s: hech kimga yetkazilmadi", key)

    # Save deduplication state
    if sent_signals > 0:
        save_state(state, state_sha)
        logging.info("V4 state GitHub'ga saqlandi")

    logging.info(
        "V4 yakunlandi. Yangi signallar: %s",
        sent_signals
    )


if __name__ == "__main__":
    main()
      
