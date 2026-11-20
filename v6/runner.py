
import base64
import html
import json
import logging
import os
from pathlib import Path

import requests

from .config import SYMBOLS, TIMEFRAMES
from .data_adapter import get_market_data
from .signal_engine import analyze_symbol


# =========================================================
# V6 UNIFIED RUNNER
# =========================================================

VERSION = "V6"
CANDLE_LIMIT = 250
MINIMUM_CANDLES = 200
REQUEST_TIMEOUT = 25
MAX_STORED_SIGNALS = 1000

TELEGRAM_ENABLED = (
    os.getenv("V6_ENABLE_TELEGRAM", "false").lower() == "true"
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WORKER_URL = os.getenv("CLOUDFLARE_WORKER_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
GITHUB_BRANCH = os.getenv("GITHUB_REF_NAME", "main")

STATE_FILE = "v6_signal_state.json"

session = requests.Session()
session.headers.update({
    "User-Agent": "ForexSignalsBot-V6/4.0"
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger("V6")


# =========================================================
# CONFIGURATION CHECK
# =========================================================

def validate_environment():
    if not TELEGRAM_ENABLED:
        logger.info("Telegram dry-run rejimida.")

    if TELEGRAM_ENABLED:
        required = {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_CHAT_ID": ADMIN_ID,
            "CLOUDFLARE_WORKER_URL": WORKER_URL,
            "WEBHOOK_SECRET": WEBHOOK_SECRET,
            "GITHUB_TOKEN": GITHUB_TOKEN,
            "GITHUB_REPOSITORY": GITHUB_REPOSITORY,
        }

        missing = [
            name for name, value in required.items() if not value
        ]

        if missing:
            raise RuntimeError(
                "Kerakli GitHub Secrets/environment yo'q: "
                + ", ".join(missing)
            )


# =========================================================
# APPROVED USERS — V4 DELIVERY INTEGRATION
# =========================================================

def get_users():
    if not WORKER_URL or not WEBHOOK_SECRET or not ADMIN_ID:
        raise RuntimeError(
            "Foydalanuvchilar ro'yxati uchun sozlamalar yetishmaydi"
        )

    response = session.get(
        f"{WORKER_URL}/approved-users",
        headers={"X-Worker-Secret": WEBHOOK_SECRET},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    payload = response.json()

    users = payload.get("users", [])

    if not isinstance(users, list):
        raise RuntimeError(
            "Worker foydalanuvchilar ro'yxatini noto'g'ri qaytardi"
        )

    return sorted(
        set([str(ADMIN_ID)] + [str(user) for user in users])
    )


# =========================================================
# TELEGRAM DELIVERY — V4 METHOD
# =========================================================

def send_telegram(chat_id, message):
    if not TELEGRAM_ENABLED:
        logger.info("Telegram o'chiq: yuborish bajarilmadi.")
        return False

    response = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": str(chat_id),
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API xatosi: {result}"
        )

    return True


# =========================================================
# PERSISTENT STATE — GITHUB CONTENTS API
# =========================================================

def state_url():
    return (
        f"https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/contents/{STATE_FILE}"
    )


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_state():
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        raise RuntimeError(
            "GitHub state uchun GITHUB_TOKEN yoki repository yo'q"
        )

    response = session.get(
        state_url(),
        headers=github_headers(),
        params={"ref": GITHUB_BRANCH},
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code == 404:
        return {"signals": {}}, None

    response.raise_for_status()

    data = response.json()

    raw = base64.b64decode(
        data["content"]
    ).decode("utf-8")

    state = json.loads(raw)

    if not isinstance(state, dict):
        raise RuntimeError("V6 state formati noto'g'ri")

    state.setdefault("signals", {})

    if not isinstance(state["signals"], dict):
        raise RuntimeError("V6 signals formati noto'g'ri")

    return state, data["sha"]


def save_state(state, sha):
    raw = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    payload = {
        "message": "Update V6 signal delivery state",
        "content": base64.b64encode(raw).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }

    if sha:
        payload["sha"] = sha

    response = session.put(
        state_url(),
        headers=github_headers(),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    result = response.json()
    content = result.get("content") or {}

    new_sha = content.get("sha")

    if not new_sha:
        raise RuntimeError(
            "GitHub state saqlandi, lekin yangi SHA qaytmadi"
        )

    return new_sha


# =========================================================
# SIGNAL DEDUPLICATION
# =========================================================

def make_signal_key(result, candles_by_timeframe):
    signal = result.signal

    m15_candles = candles_by_timeframe["M15"]
    candle_timestamp = m15_candles[-1].timestamp

    key = f"{signal.symbol}|{signal.timeframe}"

    signature = (
        f"{candle_timestamp}|{signal.side}"
    )

    return key, signature


def get_delivery_record(state, key, signature, users):
    old = state["signals"].get(key)

    if isinstance(old, dict) and old.get("signature") == signature:
        delivered = {
            str(user)
            for user in old.get("delivered_to", [])
        }

        return {
            "signature": signature,
            "target_users": list(users),
            "delivered_to": sorted(
                delivered.intersection(set(users))
            ),
        }

    return {
        "signature": signature,
        "target_users": list(users),
        "delivered_to": [],
    }


# =========================================================
# SIGNAL MESSAGE
# =========================================================

def format_price(symbol, value):
    if value is None:
        return "N/A"

    if symbol in ("EURUSD", "GBPUSD"):
        digits = 5
    elif symbol in ("XAUUSD", "XAGUSD"):
        digits = 3
    elif symbol in ("BTCUSD", "ETHUSD"):
        digits = 2
    else:
        digits = 2

    return f"{value:.{digits}f}"


def format_signal(result):
    signal = result.signal

    emoji = "🟢" if signal.side == "BUY" else "🔴"

    rr = (
        f"1:{signal.risk_reward:.2f}"
        if signal.risk_reward is not None
        else "N/A"
    )

    reason = html.escape(
        signal.reason or "Tasdiqlangan texnik shartlar"
    )

    return (
        f"{emoji} <b>FOREX SIGNALS {VERSION}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Instrument: <b>{html.escape(signal.symbol)}</b>\n"
        f"Yo'nalish: <b>{signal.side}</b>\n"
        f"Timeframe: <b>{signal.timeframe}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Entry: <code>{format_price(signal.symbol, signal.entry)}</code>\n"
        f"Stop Loss: <code>{format_price(signal.symbol, signal.stop_loss)}</code>\n"
        f"Take Profit: <code>{format_price(signal.symbol, signal.take_profit)}</code>\n"
        f"Risk/Reward: <b>{rr}</b>\n"
        f"Texnik baho: <b>{signal.confidence:.2f}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"MTF yo'nalishi: <b>{result.trend}</b>\n"
        f"MTF mosligi: <b>{result.strength:.2f}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Tasdiqlar:</b>\n{reason}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>V6 avtomatik tahlili. "
        f"Bu foyda kafolati emas.</i>"
    )


# =========================================================
# MARKET DATA
# =========================================================

def fetch_symbol_data(symbol):
    candles_by_timeframe = {}

    for timeframe in TIMEFRAMES:
        logger.info(
            "[DATA] %s %s yuklanmoqda",
            symbol,
            timeframe,
        )

        market = get_market_data(
            symbol=symbol,
            timeframe=timeframe,
            limit=CANDLE_LIMIT,
        )

        candles = market.candles

        if len(candles) < MINIMUM_CANDLES:
            raise RuntimeError(
                f"{symbol} {timeframe}: "
                f"shamlar yetarli emas: {len(candles)}"
            )

        if not all(candle.is_closed for candle in candles):
            raise RuntimeError(
                f"{symbol} {timeframe}: yopilmagan sham bor"
            )

        candles_by_timeframe[timeframe] = candles

        logger.info(
            "[DATA OK] %s %s | candles=%s | source=%s",
            symbol,
            timeframe,
            len(candles),
            candles[-1].source,
        )

    return candles_by_timeframe


# =========================================================
# V6 ANALYSIS — SINGLE DECISION ENGINE
# =========================================================

def analyze_one_symbol(symbol):
    candles_by_timeframe = fetch_symbol_data(symbol)

    result = analyze_symbol(
        symbol=symbol,
        candles_by_timeframe=candles_by_timeframe,
    )

    logger.info(
        "[V6 RESULT] %s | %s | trend=%s | strength=%.2f%% | %s",
        symbol,
        result.signal.side,
        result.trend,
        result.strength,
        result.signal.reason,
    )

    return result, candles_by_timeframe


# =========================================================
# MAIN RUNNER
# =========================================================

def main():
    validate_environment()

    logger.info("=" * 60)
    logger.info("V6 UNIFIED RUNNER ISHGA TUSHDI")
    logger.info("Instrumentlar: %s", SYMBOLS)
    logger.info("Timeframes: %s", TIMEFRAMES)
    logger.info("Telegram: %s", TELEGRAM_ENABLED)
    logger.info("=" * 60)

    
    if TELEGRAM_ENABLED:
        state, state_sha = load_state()
    else:
        state, state_sha = {"signals": {}}, None

    users = get_users() if TELEGRAM_ENABLED else []


    if TELEGRAM_ENABLED and not users:
        raise RuntimeError(
            "Telegram uchun tasdiqlangan foydalanuvchilar topilmadi"
        )

    logger.info("Telegram qabul qiluvchilar: %s", len(users))

    stats = {
        "analyzed": 0,
        "buy": 0,
        "sell": 0,
        "hold": 0,
        "failed": 0,
        "sent": 0,
        "duplicates": 0,
    }

    for symbol in SYMBOLS:
        logger.info("[SYMBOL START] %s", symbol)

        try:
            result, candles_by_timeframe = analyze_one_symbol(symbol)
            stats["analyzed"] += 1

            signal = result.signal

            if signal.side == "HOLD":
                stats["hold"] += 1
                logger.info("[HOLD] %s | %s", symbol, signal.reason)
                continue

            if signal.side == "BUY":
                stats["buy"] += 1
            elif signal.side == "SELL":
                stats["sell"] += 1
            else:
                logger.warning(
                    "Noma'lum signal: %s",
                    signal.side,
                )
                continue

            key, signature = make_signal_key(
                result,
                candles_by_timeframe,
            )

            record = get_delivery_record(
                state,
                key,
                signature,
                users,
            )

            message = format_signal(result)

            if not TELEGRAM_ENABLED:
                logger.info(
                    "[DRY RUN]\n%s",
                    message,
                )
                continue

            pending_users = [
                user
                for user in record["target_users"]
                if user not in record["delivered_to"]
            ]

            if not pending_users:
                stats["duplicates"] += 1
                logger.info(
                    "[DUPLICATE] %s %s",
                    symbol,
                    signal.side,
                )
                continue

            for user in pending_users:
                try:
                    send_telegram(user, message)

                    record["delivered_to"].append(user)

                    state["signals"][key] = record

                    state_sha = save_state(
                        state,
                        state_sha,
                    )

                    stats["sent"] += 1

                    logger.info(
                        "[SENT] %s %s -> %s",
                        symbol,
                        signal.side,
                        user,
                    )

                except Exception:
                    logger.exception(
                        "[TELEGRAM ERROR] %s -> %s",
                        symbol,
                        user,
                    )

        except Exception:
            stats["failed"] += 1
            logger.exception(
                "[SYMBOL ERROR] %s",
                symbol,
            )

    # State hajmini cheklash.
    if len(state["signals"]) > MAX_STORED_SIGNALS:
        state["signals"] = dict(
            list(state["signals"].items())[-MAX_STORED_SIGNALS:]
        )

        state_sha = save_state(state, state_sha)

    logger.info("=" * 60)
    logger.info("V6 YAKUNIY NATIJA: %s", stats)
    logger.info("=" * 60)

    return stats


if __name__ == "__main__":
    main()
