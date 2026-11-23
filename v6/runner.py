
import base64
import html
import json
import logging
import os
import sys

import requests

from .config import SYMBOLS, TIMEFRAMES
from .data_adapter import get_market_data
from .signal_engine import analyze_symbol


VERSION = "V6"
CANDLE_LIMIT = 250
MINIMUM_CANDLES = 200
REQUEST_TIMEOUT = 25
MAX_STORED_SIGNALS = 1000

TELEGRAM_ENABLED = (
    os.getenv("V6_ENABLE_TELEGRAM", "false").strip().lower() == "true"
)
TELEGRAM_SELF_TEST = (
    os.getenv("V6_TELEGRAM_SELF_TEST", "false").strip().lower() == "true"
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
session.headers.update({"User-Agent": "ForexSignalsBot-V6/6.0"})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("V6")


class StatePersistenceError(RuntimeError):
    """Signal yuborildi, ammo yuborish holati saqlanmadi."""


def validate_environment():
    if not TELEGRAM_ENABLED:
        logger.info("Telegram dry-run rejimida.")
        return

    if TELEGRAM_SELF_TEST:
        required = {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_CHAT_ID": ADMIN_ID,
        }
    else:
        required = {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_CHAT_ID": ADMIN_ID,
            "CLOUDFLARE_WORKER_URL": WORKER_URL,
            "WEBHOOK_SECRET": WEBHOOK_SECRET,
            "GITHUB_TOKEN": GITHUB_TOKEN,
            "GITHUB_REPOSITORY": GITHUB_REPOSITORY,
        }

    missing = [
        name for name, value in required.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Yetishmayotgan sozlamalar: " + ", ".join(missing)
        )


def send_telegram(chat_id, message):
    if not TELEGRAM_ENABLED:
        logger.info("Telegram o'chiq — yuborish bajarilmadi.")
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
            "Telegram API xatosi: " + str(result)
        )

    return True


def run_telegram_self_test():
    if not TELEGRAM_ENABLED:
        raise RuntimeError(
            "Telegram self-test uchun Telegram yoqilgan bo'lishi kerak."
        )

    if not ADMIN_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID mavjud emas.")

    message = (
        "✅ <b>FOREX SIGNALS V6 — TELEGRAM TEST</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Bot Telegram API bilan muvaffaqiyatli bog'landi.\n"
        "Admin chatiga test xabari yetkazildi.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Bu diagnostika xabari. Savdo signali emas.</i>"
    )

    sent = send_telegram(ADMIN_ID, message)

    if not sent:
        raise RuntimeError("Telegram test xabari yuborilmadi.")

    logger.info(
        "[TELEGRAM SELF TEST OK] Test faqat admin chatiga yuborildi."
    )

    return {
        "analyzed": 0,
        "buy": 0,
        "sell": 0,
        "hold": 0,
        "failed": 0,
        "delivery_failed": 0,
        "sent": 1,
        "duplicates": 0,
    }


def get_users():
    if not WORKER_URL or not WEBHOOK_SECRET or not ADMIN_ID:
        raise RuntimeError(
            "Worker yoki admin sozlamalari yetishmaydi."
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
            "Worker users ro'yxatini noto'g'ri qaytardi."
        )

    normalized = {
        str(user).strip()
        for user in users
        if user is not None and str(user).strip()
    }

    normalized.add(str(ADMIN_ID).strip())

    return sorted(normalized)


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


def validate_state(state):
    if not isinstance(state, dict):
        raise RuntimeError("V6 state formati noto'g'ri.")

    signals = state.setdefault("signals", {})

    if not isinstance(signals, dict):
        raise RuntimeError("V6 signals formati noto'g'ri.")

    for key, record in signals.items():
        if not isinstance(key, str) or not isinstance(record, dict):
            raise RuntimeError(
                "V6 state ichida noto'g'ri signal yozuvi."
            )

        delivered = record.get("delivered_to", [])

        if not isinstance(delivered, list):
            raise RuntimeError(
                "delivered_to ro'yxati noto'g'ri."
            )

    return state


def load_state():
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        raise RuntimeError(
            "GitHub state sozlamalari yetishmaydi."
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

    try:
        raw = base64.b64decode(data["content"]).decode("utf-8")
        state = json.loads(raw)
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            "GitHub state faylini o'qib bo'lmadi."
        ) from exc

    return validate_state(state), data["sha"]


def save_state(state, sha):
    validate_state(state)

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
    new_sha = (result.get("content") or {}).get("sha")

    if not new_sha:
        raise RuntimeError(
            "GitHub yangi state SHA qaytarmadi."
        )

    return new_sha


def make_signal_key(result, candles_by_timeframe):
    signal = result.signal
    candles = candles_by_timeframe.get("M15", [])

    if not candles:
        raise RuntimeError("M15 shamlar ro'yxati bo'sh.")

    timestamp = candles[-1].timestamp

    return (
        f"{signal.symbol}|{signal.timeframe}",
        f"{timestamp}|{signal.side}",
    )


def get_delivery_record(state, key, signature, users):
    old = state["signals"].get(key)

    if isinstance(old, dict) and old.get("signature") == signature:
        delivered = old.get("delivered_to", [])

        if not isinstance(delivered, list):
            delivered = []

        delivered = {
            str(user).strip()
            for user in delivered
            if str(user).strip()
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


def format_price(symbol, value):
    if value is None:
        return "N/A"

    if symbol in ("EURUSD", "GBPUSD"):
        digits = 5
    elif symbol in ("XAUUSD", "XAGUSD"):
        digits = 3
    else:
        digits = 2

    return f"{value:.{digits}f}"


def format_signal(result):
    """Admin uchun to'liq signal."""
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
        f"Yo'nalish: <b>{html.escape(signal.side)}</b>\n"
        f"Timeframe: <b>{html.escape(signal.timeframe)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Entry: <code>{format_price(signal.symbol, signal.entry)}</code>\n"
        f"Stop Loss: <code>{format_price(signal.symbol, signal.stop_loss)}</code>\n"
        f"Take Profit: <code>{format_price(signal.symbol, signal.take_profit)}</code>\n"
        f"Risk/Reward: <b>{rr}</b>\n"
        f"Texnik baho: <b>{signal.confidence:.2f}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"MTF yo'nalishi: <b>{html.escape(str(result.trend))}</b>\n"
        f"MTF mosligi: <b>{result.strength:.2f}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Tasdiqlar:</b>\n{reason}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>V6 avtomatik tahlili. Bu foyda kafolati emas.</i>"
    )


def format_public_signal(result):
    """Boshqa foydalanuvchilar uchun faqat savdo parametrlari."""
    signal = result.signal

    emoji = "🟢" if signal.side == "BUY" else "🔴"

    return (
        f"{emoji} <b>{html.escape(signal.symbol)} "
        f"{html.escape(signal.side)}</b>\n"
        f"Kirish: <code>{format_price(signal.symbol, signal.entry)}</code>\n"
        f"SL: <code>{format_price(signal.symbol, signal.stop_loss)}</code>\n"
        f"TP: <code>{format_price(signal.symbol, signal.take_profit)}</code>"
    )


def format_signal_for_user(result, user_id):
    """Admin va boshqa foydalanuvchilar uchun alohida format."""
    if str(user_id).strip() == str(ADMIN_ID).strip():
        return format_signal(result)

    return format_public_signal(result)


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
                f"{symbol} {timeframe}: shamlar yetarli emas "
                f"({len(candles)})"
            )

        if not all(candle.is_closed for candle in candles):
            raise RuntimeError(
                f"{symbol} {timeframe}: yopilmagan sham aniqlandi."
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


def main():
    validate_environment()

    if TELEGRAM_SELF_TEST:
        return run_telegram_self_test()

    logger.info("=" * 60)
    logger.info("V6 UNIFIED RUNNER ISHGA TUSHDI")
    logger.info("Instrumentlar: %s", SYMBOLS)
    logger.info("Timeframes: %s", TIMEFRAMES)
    logger.info("Telegram: %s", TELEGRAM_ENABLED)
    logger.info("=" * 60)

    if TELEGRAM_ENABLED:
        state, state_sha = load_state()
        users = get_users()

        if not users:
            raise RuntimeError(
                "Tasdiqlangan foydalanuvchilar topilmadi."
            )
    else:
        state, state_sha = {"signals": {}}, None
        users = []

    stats = {
        "analyzed": 0,
        "buy": 0,
        "sell": 0,
        "hold": 0,
        "failed": 0,
        "delivery_failed": 0,
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
                logger.info(
                    "[HOLD] %s | %s",
                    symbol,
                    signal.reason,
                )
                continue

            if signal.side == "BUY":
                stats["buy"] += 1
            elif signal.side == "SELL":
                stats["sell"] += 1
            else:
                raise RuntimeError(
                    f"Noma'lum signal yo'nalishi: {signal.side}"
                )

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

            if not TELEGRAM_ENABLED:
                logger.info(
                    "[DRY RUN]\n%s",
                    format_signal(result),
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
                    message = format_signal_for_user(result, user)

                    if not send_telegram(user, message):
                        raise RuntimeError(
                            "Telegram yuborishni tasdiqlamadi."
                        )

                except Exception:
                    stats["delivery_failed"] += 1
                    logger.exception(
                        "[TELEGRAM ERROR] %s -> %s",
                        symbol,
                        user,
                    )
                    continue

                record["delivered_to"].append(user)
                record["delivered_to"] = sorted(
                    set(record["delivered_to"])
                )

                state["signals"][key] = record

                try:
                    state_sha = save_state(state, state_sha)

                except Exception as exc:
                    logger.exception("[STATE SAVE ERROR]")

                    raise StatePersistenceError(
                        "Signal yuborildi, lekin state saqlanmadi. "
                        "Takroriy yuborish xavfi sabab runner to'xtatildi."
                    ) from exc

                stats["sent"] += 1

                logger.info(
                    "[SENT] %s %s -> %s",
                    symbol,
                    signal.side,
                    user,
                )

        except StatePersistenceError:
            raise

        except Exception:
            stats["failed"] += 1
            logger.exception("[SYMBOL ERROR] %s", symbol)

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
    final_stats = main()

    if final_stats["failed"] or final_stats["delivery_failed"]:
        logger.error("V6 xatoliklar bilan yakunlandi.")
        sys.exit(1)
