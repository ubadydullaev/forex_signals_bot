
import os
import json
import time
import base64
import logging
from datetime import datetime, timedelta, timezone

import requests
import subprocess
# ================= CONFIG =================

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = str(os.environ["TELEGRAM_CHAT_ID"])
WORKER_URL = os.environ["CLOUDFLARE_WORKER_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPOSITORY = os.environ["GITHUB_REPOSITORY"]
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")


STATE_FILE = "bot_v4_state.json"

SYMBOLS = [
    "XAUUSD", "EURUSD", "GBPUSD", "USDJPY",
    "AUDUSD", "USDCAD", "USDCHF"
]

TIMEFRAMES = {
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
}

ATR_PERIOD = 14
MIN_SCORE = 78
MIN_REWARD_RISK = 1.5
SL_ATR_MIN = 1.2
SL_ATR_MAX = 2.2
TP_R_MULTIPLES = (1.5, 2.5, 3.5)
REFERENCE_LOT = 0.01
CANDLE_LIMIT = 1000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
session = requests.Session()
session.headers.update({"User-Agent": "ForexSignalsBot/4.1"})


# ================= MARKET DATA =================

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
    timeframe_map = {
        "15m": ("m15", 7, 15 * 60 * 1000),
        "30m": ("m30", 10, 30 * 60 * 1000),
        "1h": ("h1", 20, 60 * 60 * 1000),
        "4h": ("h4", 75, 4 * 60 * 60 * 1000),
    }

    if interval not in timeframe_map:
        raise ValueError(f"Noto'g'ri timeframe: {interval}")

    dukascopy_tf, lookback_days, candle_ms = timeframe_map[interval]

    now = datetime.now(timezone.utc)
    from_date = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    output_dir = os.path.abspath("download")
    os.makedirs(output_dir, exist_ok=True)

    file_name = (
        f"{symbol.lower()}_{dukascopy_tf}_"
        f"{now.strftime('%Y%m%d%H%M')}"
    )

    output_file = os.path.join(output_dir, file_name + ".json")

    command = [
        "npx", "--no-install", "dukascopy-node",
        "-i", symbol.lower(),
        "-from", from_date,
        "-to", to_date,
        "-t", dukascopy_tf,
        "-f", "json",
        "-dir", output_dir,
        "-fn", file_name,
        "--cache",
        "--retries", "3",
        
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=240
    )

    if result.returncode != 0:
        diagnostic = "\n".join(
            x for x in (result.stdout, result.stderr)
            if x and x.strip()
        )
        raise RuntimeError(
            f"Dukascopy xatosi: {diagnostic[-2500:] or 'CLI hech qanday xabar bermadi'}"
        )

    if not os.path.exists(output_file):
        raise RuntimeError("Dukascopy JSON fayl yaratilmadi")

    with open(output_file, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    if not isinstance(raw_data, list):
        raise RuntimeError("Dukascopy noto'g'ri JSON format qaytardi")

    now_ms = int(now.timestamp() * 1000)
    candles = []

    for item in raw_data:
        try:
            timestamp = int(item["timestamp"])

            # Faqat to'liq yopilgan shamlar
            if timestamp + candle_ms > now_ms:
                continue

            candle = {
                "time": datetime.fromtimestamp(
                    timestamp / 1000,
                    timezone.utc
                ).isoformat(),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            }

            if not all(
                value > 0
                for value in (
                    candle["open"],
                    candle["high"],
                    candle["low"],
                    candle["close"]
                )
            ):
                continue

            if candle["high"] < candle["low"]:
                continue

            candles.append(candle)

        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    candles.sort(key=lambda x: time_key(x["time"]))

    if len(candles) < 220:
        raise RuntimeError(
            f"Yetarli yopilgan sham yo'q: {len(candles)}/220"
        )

    return candles[-1000:]






# ================= INDICATORS =================

def ema_series(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    current = sum(values[:period]) / period
    out[period - 1] = current
    alpha = 2 / (period + 1)
    for i in range(period, len(values)):
        current = values[i] * alpha + current * (1 - alpha)
        out[i] = current
    return out


def rsi_series(values, period=14):
    out = [None] * len(values)
    if len(values) <= period:
        return out
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    def calc(g, l):
        if l == 0:
            return 100.0
        return 100 - 100 / (1 + g / l)

    out[period] = calc(avg_g, avg_l)
    for i in range(period + 1, len(values)):
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        out[i] = calc(avg_g, avg_l)
    return out


def true_ranges(candles):
    result = []
    for i, c in enumerate(candles):
        if i == 0:
            result.append(c["high"] - c["low"])
        else:
            prev = candles[i - 1]["close"]
            result.append(max(
                c["high"] - c["low"],
                abs(c["high"] - prev),
                abs(c["low"] - prev)
            ))
    return result


def atr_series(candles, period=14):
    tr = true_ranges(candles)
    out = [None] * len(candles)
    if len(tr) < period:
        return out
    value = sum(tr[:period]) / period
    out[period - 1] = value
    for i in range(period, len(tr)):
        value = (value * (period - 1) + tr[i]) / period
        out[i] = value
    return out



def adx_value(candles, period=14):
    if len(candles) < period * 2 + 1:
        return None

    tr = true_ranges(candles)
    plus_dm = [0.0] * len(candles)
    minus_dm = [0.0] * len(candles)

    for i in range(1, len(candles)):
        up = candles[i]["high"] - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - candles[i]["low"]

        if up > down and up > 0:
            plus_dm[i] = up
        elif down > up and down > 0:
            minus_dm[i] = down

    smoothed_tr = sum(tr[1:period + 1])
    smoothed_plus = sum(plus_dm[1:period + 1])
    smoothed_minus = sum(minus_dm[1:period + 1])

    dx_values = []

    for i in range(period, len(candles)):
        if i > period:
            smoothed_tr = (
                smoothed_tr - smoothed_tr / period + tr[i]
            )
            smoothed_plus = (
                smoothed_plus - smoothed_plus / period + plus_dm[i]
            )
            smoothed_minus = (
                smoothed_minus - smoothed_minus / period + minus_dm[i]
            )

        if smoothed_tr <= 0:
            dx_values.append(0.0)
            continue

        plus_di = 100 * smoothed_plus / smoothed_tr
        minus_di = 100 * smoothed_minus / smoothed_tr

        denominator = plus_di + minus_di

        dx = (
            100 * abs(plus_di - minus_di) / denominator
            if denominator > 0 else 0.0
        )

        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    adx = sum(dx_values[:period]) / period

    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period

    return adx



def supertrend_direction(candles, atrs, factor=2.5):
    final_upper = final_lower = None
    direction = 0
    for i, c in enumerate(candles):
        atr = atrs[i]
        if atr is None:
            continue
        mid = (c["high"] + c["low"]) / 2
        basic_upper = mid + factor * atr
        basic_lower = mid - factor * atr
        if final_upper is None:
            final_upper, final_lower = basic_upper, basic_lower
            direction = 1 if c["close"] >= mid else -1
            continue
        prev_close = candles[i - 1]["close"]
        final_upper = (
            basic_upper if basic_upper < final_upper or prev_close > final_upper
            else final_upper
        )
        final_lower = (
            basic_lower if basic_lower > final_lower or prev_close < final_lower
            else final_lower
        )
        if direction == -1 and c["close"] > final_upper:
            direction = 1
        elif direction == 1 and c["close"] < final_lower:
            direction = -1
    return direction


# ================= STRUCTURE / DIRECTION =================

def trend_direction(candles):
    closes = [x["close"] for x in candles]
    e20 = ema_series(closes, 20)[-1]
    e50 = ema_series(closes, 50)[-1]
    e200 = ema_series(closes, 200)[-1]
    if None in (e20, e50, e200):
        return 0
    if e20 > e50 > e200 and closes[-1] > e20:
        return 1
    if e20 < e50 < e200 and closes[-1] < e20:
        return -1
    return 0


def structure_data(candles):
    recent = candles[-21:-1]
    c = candles[-1]
    if len(recent) < 15:
        return {"buy": 0, "sell": 0, "swing_low": None, "swing_high": None}
    high = max(x["high"] for x in recent)
    low = min(x["low"] for x in recent)
    body = c["close"] - c["open"]

    buy = 0
    sell = 0
    if c["low"] < low and c["close"] > low:
        buy += 1
    if c["high"] > high and c["close"] < high:
        sell += 1
    if c["close"] > high:
        buy += 2
    if c["close"] < low:
        sell += 2
    if len(candles) >= 3:
        if c["low"] > candles[-3]["high"]:
            buy += 1
        if c["high"] < candles[-3]["low"]:
            sell += 1
    if body > 0:
        buy += 1
    elif body < 0:
        sell += 1
    return {
        "buy": buy, "sell": sell,
        "swing_low": low, "swing_high": high
    }


def price_digits(symbol):
    if symbol.endswith("JPY"):
        return 3
    if symbol == "XAUUSD":
        return 2
    return 5


# ================= USD RISK ESTIMATE =================

def usd_value_per_price_unit(symbol, entry):
    """
    Approximate USD P/L for a 1.0 price move at 0.01 standard FX lot.
    Assumptions: FX contract=100,000 units; XAUUSD=100 oz per lot.
    Broker contract specifications may differ.
    """
    if symbol == "XAUUSD":
        return 1.0
    if symbol in ("EURUSD", "GBPUSD", "AUDUSD"):
        return 1000.0
    if symbol == "USDJPY":
        return 1000.0 / entry
    if symbol == "USDCHF":
        return 1000.0 / entry
    if symbol == "USDCAD":
        return 1000.0 / entry
    return None


def analyze(symbol, timeframe, candles, h1, h4):
    closes = [x["close"] for x in candles]
    current = candles[-1]
    e20 = ema_series(closes, 20)[-1]
    e50 = ema_series(closes, 50)[-1]
    e200 = ema_series(closes, 200)[-1]
    rsi = rsi_series(closes)[-1]
    atrs = atr_series(candles, ATR_PERIOD)
    atr = atrs[-1]
    adx = adx_value(candles)

    if any(x is None for x in (e20, e50, e200, rsi, atr, adx)):
        return None
    if atr <= 0 or adx < 18:
        return None

    st = supertrend_direction(candles, atrs)
    h1_dir, h4_dir = trend_direction(h1), trend_direction(h4)
    structure = structure_data(candles)

    buy = sell = 0
    if e20 > e50:
        buy += 15
    elif e20 < e50:
        sell += 15

    if current["close"] > e200:
        buy += 10
    elif current["close"] < e200:
        sell += 10

    if st == 1:
        buy += 12
    elif st == -1:
        sell += 12

    if 52 <= rsi <= 68:
        buy += 10
    elif 32 <= rsi <= 48:
        sell += 10

    if adx >= 22:
        if e20 > e50:
            buy += 10
        elif e20 < e50:
            sell += 10

    if h1_dir == 1:
        buy += 10
    elif h1_dir == -1:
        sell += 10
    if h4_dir == 1:
        buy += 10
    elif h4_dir == -1:
        sell += 10

    if structure["buy"]:
        buy += min(15, structure["buy"] * 5)
    if structure["sell"]:
        sell += min(15, structure["sell"] * 5)

    if buy >= MIN_SCORE and buy > sell and h1_dir == 1 and h4_dir == 1:
        side, score = "BUY", buy
    elif sell >= MIN_SCORE and sell > buy and h1_dir == -1 and h4_dir == -1:
        side, score = "SELL", sell
    else:
        return None

    if side == "BUY":
        structural_sl = structure["swing_low"]
        atr_sl = current["close"] - atr * 1.5
        sl = min(atr_sl, structural_sl) if structural_sl else atr_sl
        risk = current["close"] - sl
    else:
        structural_sl = structure["swing_high"]
        atr_sl = current["close"] + atr * 1.5
        sl = max(atr_sl, structural_sl) if structural_sl else atr_sl
        risk = sl - current["close"]

    if risk <= 0 or risk < atr * SL_ATR_MIN or risk > atr * SL_ATR_MAX:
        return None

    if side == "BUY":
        tps = [current["close"] + risk * x for x in TP_R_MULTIPLES]
    else:
        tps = [current["close"] - risk * x for x in TP_R_MULTIPLES]

    if abs(tps[0] - current["close"]) / risk < MIN_REWARD_RISK:
        return None

    usd_factor = usd_value_per_price_unit(symbol, current["close"])
    if usd_factor is None:
        return None
    risk_usd = risk * usd_factor
    rewards = [risk_usd * x for x in TP_R_MULTIPLES]

    return {
        "symbol": symbol, "timeframe": timeframe, "side": side,
        "score": score, "entry": current["close"], "sl": sl,
        "tp1": tps[0], "tp2": tps[1], "tp3": tps[2],
        "risk_usd": risk_usd, "rewards_usd": rewards,
        "candle_time": current["time"], "adx": adx, "rsi": rsi,
    }


# ================= TELEGRAM =================

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
            "chat_id": str(chat_id), "text": message,
            "parse_mode": "HTML", "disable_web_page_preview": True
        },
        timeout=20
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(str(result))
    return True


def format_signal(s):
    sym = s["symbol"]
    digits = price_digits(sym)
    emoji = "🟢" if s["side"] == "BUY" else "🔴"
    fmt = lambda x: f"{x:.{digits}f}"
    rewards = s["rewards_usd"]
    return (
        f"{emoji} <b>{sym} — {s['side']}</b>\n"
        f"⏱ Timeframe: <b>{s['timeframe']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🎯 Entry: <code>{fmt(s['entry'])}</code>\n"
        f"🛑 SL: <code>{fmt(s['sl'])}</code>\n"
        f"✅ TP1: <code>{fmt(s['tp1'])}</code>  (~${rewards[0]:.2f})\n"
        f"✅ TP2: <code>{fmt(s['tp2'])}</code>  (~${rewards[1]:.2f})\n"
        f"✅ TP3: <code>{fmt(s['tp3'])}</code>  (~${rewards[2]:.2f})\n"
        f"━━━━━━━━━━━━━━\n"
        f"⚠️ 0.01 lot uchun taxminiy SL riski: "
        f"<b>${s['risk_usd']:.2f}</b>\n"
        f"📊 Confluence score: <b>{s['score']}/100</b>\n"
        f"ADX: {s['adx']:.1f} | RSI: {s['rsi']:.1f}\n"
        f"EMA + RSI + ADX + Supertrend\n"
        f"HTF trend + structure + liquidity + FVG\n"
        f"🕯 Yopilgan sham asosida\n"
        f"⚠️ Dollar hisob-kitobi taxminiy: broker kontrakt hajmi, "
        f"spread, komissiya va slippage farq qilishi mumkin.\n"
        f"⚠️ Avval demo hisobda tekshiring. Bu avtomatik savdo emas."
    )


# ================= GITHUB STATE =================

def state_url():
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
        state_url(), headers=github_headers(),
        params={"ref": BRANCH}, timeout=20
    )
    if response.status_code == 404:
        return {"signals": {}}, None
    response.raise_for_status()
    data = response.json()
    raw = base64.b64decode(data["content"]).decode("utf-8")
    state = json.loads(raw)
    state.setdefault("signals", {})
    return state, data["sha"]


def save_state(state, sha):
    raw = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    payload = {
        "message": "Update V4 signal state",
        "content": base64.b64encode(raw).decode("utf-8"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    response = session.put(
        state_url(), headers=github_headers(),
        json=payload, timeout=25
    )
    response.raise_for_status()


# ================= MAIN =================

def main():
    logging.info("Forex V4 v4.1 ishga tushdi")
    state, state_sha = load_state()
    users = get_users()
    logging.info("Qabul qiluvchilar: %s", len(users))

    cache = {}
    for symbol in SYMBOLS:
        for tf, interval in TIMEFRAMES.items():
            try:
                cache[(symbol, tf)] = get_candles(symbol, interval)
                logging.info(
                    "%s %s: %s candles",
                    symbol, tf, len(cache[(symbol, tf)])
                )
            except Exception as exc:
                logging.warning("%s %s ma'lumot olinmadi: %s", symbol, tf, exc)

    candidates = []
    for symbol in SYMBOLS:
        h1, h4 = cache.get((symbol, "H1")), cache.get((symbol, "H4"))
        if not h1 or not h4:
            logging.warning("%s: H1/H4 yo'q, instrument o'tkazildi", symbol)
            continue
        for tf in TIMEFRAMES:
            candles = cache.get((symbol, tf))
            if not candles:
                continue
            try:
                signal = analyze(symbol, tf, candles, h1, h4)
                if signal is None:
                    continue
                key = f"{symbol}_{tf}"
                signature = f"{signal['candle_time']}_{signal['side']}"
                if state["signals"].get(key) == signature:
                    continue
                candidates.append((key, signature, signal))
            except Exception:
                logging.exception("%s %s tahlil xatosi", symbol, tf)

    candidates.sort(key=lambda x: x[2]["score"], reverse=True)
    sent = 0
    for key, signature, signal in candidates:
        delivered = 0
        message = format_signal(signal)
        for uid in users:
            try:
                send_telegram(uid, message)
                delivered += 1
            except Exception:
                logging.exception("Telegram yuborilmadi: %s", uid)
            time.sleep(0.15)
        if delivered:
            state["signals"][key] = signature
            sent += 1
            logging.info("%s %s yuborildi (%s)", key, signal["side"], delivered)

    if sent:
        save_state(state, state_sha)
        logging.info("V4 state GitHub'ga saqlandi")
    logging.info("V4 yakunlandi. Yangi signallar: %s", sent)


if __name__ == "__main__":
    main()
