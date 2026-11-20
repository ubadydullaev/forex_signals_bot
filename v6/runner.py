
import html
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import SYMBOLS, TIMEFRAMES
from .data_adapter import get_market_data
from .signal_engine import analyze_symbol


# =========================================================
# V6 RUNNER
# =========================================================

VERSION = "V6"
CANDLE_LIMIT = 250
STATE_FILE = Path(
    os.getenv("V6_STATE_FILE", "v6_signal_state.json")
)

TELEGRAM_ENABLED = (
    os.getenv("V6_ENABLE_TELEGRAM", "false").lower()
    == "true"
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

REQUEST_TIMEOUT = 20
MAX_STORED_SIGNALS = 1000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger("V6")


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message: str) -> bool:
    if not TELEGRAM_ENABLED:
        logger.info("Telegram o'chiq. Xabar yuborilmadi.")
        return False

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN yoki TELEGRAM_CHAT_ID topilmadi"
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
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
# SIGNAL STATE
# =========================================================

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"sent_signals": []}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            raise ValueError("State formati noto'g'ri")

        sent = state.get("sent_signals", [])

        if not isinstance(sent, list):
            raise ValueError("sent_signals formati noto'g'ri")

        return {"sent_signals": sent}

    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(
            f"Signal state faylini o'qib bo'lmadi: {error}"
        ) from error


def save_state(state: dict) -> None:
    temporary_file = STATE_FILE.with_suffix(".tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2,
        )

    temporary_file.replace(STATE_FILE)


def make_signal_key(result) -> str:
    signal = result.signal

    candles_time = result.signal.timeframe

    return (
        f"{signal.symbol}|"
        f"{signal.side}|"
        f"{candles_time}|"
        f"{signal.entry}|"
        f"{signal.stop_loss}|"
        f"{signal.take_profit}"
    )


# =========================================================
# SIGNAL FORMAT
# =========================================================

def format_price(symbol: str, value) -> str:
    if value is None:
        return "N/A"

    if symbol in ("EURUSD", "GBPUSD"):
        digits = 5
    elif symbol in ("XAUUSD", "XAGUSD"):
        digits = 3
    else:
        digits = 2

    return f"{value:.{digits}f}"


def format_signal(result) -> str:
    signal = result.signal

    emoji = "🟢" if signal.side == "BUY" else "🔴"

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
        f"Risk/Reward: <b>1:{signal.risk_reward:.2f}</b>\n"
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

def fetch_symbol_data(symbol: str) -> dict:
    candles_by_timeframe = {}

    for timeframe in TIMEFRAMES:
        logger.info(
            "[DATA] %s %s ma'lumot olinmoqda",
            symbol,
            timeframe,
        )

        market = get_market_data(
            symbol=symbol,
            timeframe=timeframe,
            limit=CANDLE_LIMIT,
        )

        candles = market.candles

        if len(candles) < 200:
            raise RuntimeError(
                f"{symbol} {timeframe}: "
                f"shamlar yetarli emas: {len(candles)}"
            )

        if not all(candle.is_closed for candle in candles):
            raise RuntimeError(
                f"{symbol} {timeframe}: "
                "yopilmagan sham topildi"
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
# SYMBOL ANALYSIS
# =========================================================

def analyze_one_symbol(symbol: str):
    candles_by_timeframe = fetch_symbol_data(symbol)

    result = analyze_symbol(
        symbol=symbol,
        candles_by_timeframe=candles_by_timeframe,
    )

    signal = result.signal

    logger.info(
        "[V6 RESULT] %s | %s | trend=%s | strength=%.2f%% | %s",
        symbol,
        signal.side,
        result.trend,
        result.strength,
        signal.reason,
    )

    return result


# =========================================================
# RUNNER
# =========================================================

def main():
    logger.info("=" * 55)
    logger.info("V6 RUNNER ISHGA TUSHDI")
    logger.info("Instruments: %s", len(SYMBOLS))
    logger.info("Timeframes: %s", TIMEFRAMES)
    logger.info("Telegram enabled: %s", TELEGRAM_ENABLED)
    logger.info("=" * 55)

    state = load_state()
    sent_keys = set(state["sent_signals"])

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
            result = analyze_one_symbol(symbol)
            stats["analyzed"] += 1

            signal = result.signal

            if signal.side == "HOLD":
                stats["hold"] += 1
                continue

            if signal.side == "BUY":
                stats["buy"] += 1
            elif signal.side == "SELL":
                stats["sell"] += 1
            else:
                logger.warning(
                    "Noma'lum signal turi: %s",
                    signal.side,
                )
                continue

            key = make_signal_key(result)

            if key in sent_keys:
                stats["duplicates"] += 1
                logger.info(
                    "[DUPLICATE] %s signali oldin yuborilgan",
                    symbol,
                )
                continue

            message = format_signal(result)

            logger.info(
                "[SIGNAL READY] %s %s",
                symbol,
                signal.side,
            )

            if TELEGRAM_ENABLED:
                if send_telegram(message):
                    sent_keys.add(key)

                    state["sent_signals"] = list(
                        sent_keys
                    )[-MAX_STORED_SIGNALS:]

                    save_state(state)

                    stats["sent"] += 1

            else:
                logger.info(
                    "[DRY RUN]\n%s",
                    message,
                )

        except Exception:
            stats["failed"] += 1

            logger.exception(
                "[SYMBOL ERROR] %s",
                symbol,
            )

    logger.info("=" * 55)
    logger.info("V6 RUNNER YAKUNLANDI")
    logger.info("Natijalar: %s", stats)
    logger.info("=" * 55)

    return stats


if __name__ == "__main__":
    main()
                
