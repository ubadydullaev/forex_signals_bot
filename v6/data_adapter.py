
import json
import math
import os
import re
import subprocess
import tempfile

from datetime import datetime, timedelta, timezone

import requests

from .models import Candle, MarketData
from .config import SYMBOLS, TIMEFRAMES


# =========================================================
# V6 DATA ADAPTER
# =========================================================

BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"

MIN_CANDLES = 220
MAX_CANDLES = 1000
REQUEST_TIMEOUT = 30

DUKASCOPY_SYMBOLS = {
    "XAUUSD": "xauusd",
    "XAGUSD": "xagusd",
    "EURUSD": "eurusd",
    "GBPUSD": "gbpusd",
    "US500": "usa500idxusd",
}

BINANCE_SYMBOLS = {
    "BTCUSD": "BTCUSDT",
    "ETHUSD": "ETHUSDT",
}

TIMEFRAME_SETTINGS = {
    "M15": {
        "interval": "15m",
        "dukascopy_tf": "m15",
        "duration_ms": 15 * 60 * 1000,
        "lookback_days": 7,
    },
    "M30": {
        "interval": "30m",
        "dukascopy_tf": "m30",
        "duration_ms": 30 * 60 * 1000,
        "lookback_days": 10,
    },
    "H1": {
        "interval": "1h",
        "dukascopy_tf": "h1",
        "duration_ms": 60 * 60 * 1000,
        "lookback_days": 20,
    },
    "H4": {
        "interval": "4h",
        "dukascopy_tf": "h4",
        "duration_ms": 4 * 60 * 60 * 1000,
        "lookback_days": 75,
    },
    "D1": {
        "interval": "1d",
        "dukascopy_tf": "d1",
        "duration_ms": 24 * 60 * 60 * 1000,
        "lookback_days": 500,
    },
}


session = requests.Session()
session.headers.update({
    "User-Agent": "ForexSignalsBot-V6/1.0"
})


# =========================================================
# COMMON VALIDATION
# =========================================================

def _validate_request(symbol, timeframe, limit):
    if symbol not in SYMBOLS:
        raise ValueError(
            f"V6 uchun noma'lum instrument: {symbol}"
        )

    if timeframe not in TIMEFRAMES:
        raise ValueError(
            f"V6 uchun noma'lum timeframe: {timeframe}"
        )

    if not isinstance(limit, int):
        raise ValueError("limit butun son bo'lishi kerak")

    if not MIN_CANDLES <= limit <= MAX_CANDLES:
        raise ValueError(
            f"Shamlar soni {MIN_CANDLES}-{MAX_CANDLES} "
            "oralig'ida bo'lishi kerak"
        )


def _valid_prices(open_price, high, low, close):
    values = (open_price, high, low, close)

    if not all(
        math.isfinite(value) and value > 0
        for value in values
    ):
        return False

    if high < low:
        return False

    if not low <= open_price <= high:
        return False

    if not low <= close <= high:
        return False

    return True


def _build_candles(raw_candles, timeframe, source, limit):
    duration_ms = TIMEFRAME_SETTINGS[timeframe]["duration_ms"]
    now_ms = int(
        datetime.now(timezone.utc).timestamp() * 1000
    )

    candles_by_time = {}

    for item in raw_candles:
        try:
            timestamp = int(item["timestamp"])

            # Faqat to'liq yopilgan shamlar.
            if timestamp <= 0:
                continue

            if timestamp + duration_ms > now_ms:
                continue

            open_price = float(item["open"])
            high = float(item["high"])
            low = float(item["low"])
            close = float(item["close"])
            volume = float(item.get("volume", 0.0))

            if not _valid_prices(
                open_price, high, low, close
            ):
                continue

            if not math.isfinite(volume) or volume < 0:
                continue

            candles_by_time[timestamp] = Candle(
                timestamp=timestamp,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                is_closed=True,
                source=source,
            )

        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            continue

    candles = sorted(
        candles_by_time.values(),
        key=lambda candle: candle.timestamp,
    )

    if len(candles) < MIN_CANDLES:
        raise RuntimeError(
            f"{source} {timeframe}: yopilgan shamlar "
            f"yetarli emas: {len(candles)}/{MIN_CANDLES}"
        )

    return candles[-limit:]


# =========================================================
# BINANCE DATA
# =========================================================

def _fetch_binance(symbol, timeframe, limit):
    binance_symbol = BINANCE_SYMBOLS.get(symbol)

    if not binance_symbol:
        raise ValueError(
            f"Binance instrument kodi topilmadi: {symbol}"
        )

    interval = TIMEFRAME_SETTINGS[timeframe]["interval"]

    response = session.get(
        BINANCE_URL,
        params={
            "symbol": binance_symbol,
            "interval": interval,
            "limit": limit,
        },
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()
    raw_data = response.json()

    if not isinstance(raw_data, list):
        raise RuntimeError(
            "Binance noto'g'ri ma'lumot qaytardi"
        )

    raw_candles = []

    for item in raw_data:
        try:
            raw_candles.append({
                "timestamp": int(item[0]),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
            })

        except (
            IndexError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            continue

    return _build_candles(
        raw_candles=raw_candles,
        timeframe=timeframe,
        source="binance_spot",
        limit=limit,
    )


# =========================================================
# DUKASCOPY DATA
# =========================================================

def _fetch_dukascopy(symbol, timeframe, limit):
    instrument = DUKASCOPY_SYMBOLS.get(symbol)

    if not instrument:
        raise ValueError(
            f"Dukascopy instrument kodi topilmadi: {symbol}"
        )

    settings = TIMEFRAME_SETTINGS[timeframe]

    now = datetime.now(timezone.utc)

    from_date = (
        now - timedelta(days=settings["lookback_days"])
    ).strftime("%Y-%m-%d")

    to_date = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    safe_name = re.sub(
        r"[^a-zA-Z0-9_]",
        "_",
        f"v6_{instrument}_{timeframe}",
    )

    with tempfile.TemporaryDirectory(
        prefix="v6_market_"
    ) as output_dir:

        command = [
            "npx",
            "--no-install",
            "dukascopy-node",
            "-i",
            instrument,
            "-from",
            from_date,
            "-to",
            to_date,
            "-t",
            settings["dukascopy_tf"],
            "-f",
            "json",
            "-dir",
            output_dir,
            "-fn",
            safe_name,
            "--cache",
            "--retries",
            "3",
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )

        if result.returncode != 0:
            diagnostic = "\n".join(
                part for part in (
                    result.stdout,
                    result.stderr,
                )
                if part and part.strip()
            )

            raise RuntimeError(
                "Dukascopy ma'lumot olishda xato: "
                + (diagnostic[-2000:] or "Noma'lum xato")
            )

        output_file = os.path.join(
            output_dir,
            safe_name + ".json",
        )

        if not os.path.isfile(output_file):
            raise RuntimeError(
                "Dukascopy JSON fayli yaratilmadi"
            )

        with open(
            output_file,
            "r",
            encoding="utf-8",
        ) as file:
            raw_data = json.load(file)

    if not isinstance(raw_data, list):
        raise RuntimeError(
            "Dukascopy noto'g'ri JSON format qaytardi"
        )

    raw_candles = []

    for item in raw_data:
        try:
            raw_candles.append({
                "timestamp": int(item["timestamp"]),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item.get("volume", 0.0)),
            })

        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            continue

    return _build_candles(
        raw_candles=raw_candles,
        timeframe=timeframe,
        source="dukascopy",
        limit=limit,
    )


# =========================================================
# PUBLIC V6 INTERFACE
# =========================================================

def get_candles(symbol, timeframe, limit=MAX_CANDLES):
    """
    Belgilangan instrument va timeframe uchun
    standart Candle obyektlarini qaytaradi.
    """

    _validate_request(symbol, timeframe, limit)

    if symbol in BINANCE_SYMBOLS:
        return _fetch_binance(
            symbol,
            timeframe,
            limit,
        )

    if symbol in DUKASCOPY_SYMBOLS:
        return _fetch_dukascopy(
            symbol,
            timeframe,
            limit,
        )

    raise ValueError(
        f"{symbol} uchun ma'lumot manbasi aniqlanmagan"
    )


def get_market_data(symbol, timeframe, limit=MAX_CANDLES):
    """
    V6 tahlil modullari uchun MarketData obyektini qaytaradi.
    """

    candles = get_candles(
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
    )

    return MarketData(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
      )
  
