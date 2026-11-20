
import json
import math
import os
import re
import subprocess
import tempfile
import warnings

from datetime import datetime, timedelta, timezone, time as dt_time
from zoneinfo import ZoneInfo

import requests

from .models import Candle, MarketData
from .config import SYMBOLS, TIMEFRAMES


# ==========================================
# V6 MARKET DATA ADAPTER
# ==========================================

BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"

MIN_CANDLES = 220
MAX_CANDLES = 10000
BINANCE_MAX_PAGE = 1000

REQUEST_TIMEOUT = 30
DUKASCOPY_TIMEOUT = 240

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
    },
    "M30": {
        "interval": "30m",
        "dukascopy_tf": "m30",
        "duration_ms": 30 * 60 * 1000,
    },
    "H1": {
        "interval": "1h",
        "dukascopy_tf": "h1",
        "duration_ms": 60 * 60 * 1000,
    },
    "H4": {
        "interval": "4h",
        "dukascopy_tf": "h4",
        "duration_ms": 4 * 60 * 60 * 1000,
    },
    "D1": {
        "interval": "1d",
        "dukascopy_tf": "d1",
        "duration_ms": 24 * 60 * 60 * 1000,
    },
}

session = requests.Session()
session.headers.update({
    "User-Agent": "ForexSignalsBot-V6/4.0"
})


# ==========================================
# REQUEST VALIDATION
# ==========================================

def _validate_request(symbol, timeframe, limit):
    if symbol not in SYMBOLS:
        raise ValueError(
            f"V6 uchun noma'lum instrument: {symbol}"
        )

    if timeframe not in TIMEFRAMES:
        raise ValueError(
            f"V6 uchun noma'lum timeframe: {timeframe}"
        )

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
    ):
        raise ValueError(
            "limit musbat butun son bo'lishi kerak"
        )

    if not MIN_CANDLES <= limit <= MAX_CANDLES:
        raise ValueError(
            f"Shamlar soni {MIN_CANDLES}-{MAX_CANDLES} "
            "oralig'ida bo'lishi kerak"
        )


# ==========================================
# DATA VALIDATION
# ==========================================

def _valid_prices(open_price, high, low, close):
    values = (
        open_price,
        high,
        low,
        close,
    )

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


def _valid_volume(volume):
    return (
        math.isfinite(volume)
        and volume >= 0
    )


def _valid_timestamp(timestamp):
    return (
        not isinstance(timestamp, bool)
        and isinstance(timestamp, int)
        and timestamp >= 100_000_000_000
    )


# ==========================================
# HISTORICAL RANGE
# ==========================================

def _calculate_lookback_days(timeframe, limit):
    duration_ms = TIMEFRAME_SETTINGS[
        timeframe
    ]["duration_ms"]

    seconds_per_candle = duration_ms / 1000

    estimated_days = (
        limit
        * seconds_per_candle
        / 86400
        * (7 / 5)
        * 1.35
    )

    return max(
        7,
        math.ceil(estimated_days) + 2,
    )


# ==========================================
# MARKET CLOSURE CHECK
# ==========================================

def _is_expected_market_closure(timestamp, symbol):
    if symbol in ("XAUUSD", "XAGUSD"):
        market_tz = ZoneInfo("America/New_York")
        close_time = dt_time(17, 0)
        open_time = dt_time(18, 0)

    elif symbol == "US500":
        market_tz = ZoneInfo("America/Chicago")
        close_time = dt_time(15, 15)
        open_time = dt_time(17, 0)

    elif symbol in ("EURUSD", "GBPUSD"):
        market_tz = ZoneInfo("America/New_York")
        close_time = dt_time(17, 0)
        open_time = dt_time(17, 0)

    else:
        return False

    local_dt = datetime.fromtimestamp(
        timestamp / 1000,
        tz=timezone.utc,
    ).astimezone(market_tz)

    weekday = local_dt.weekday()
    local_time = local_dt.time().replace(tzinfo=None)

    if weekday == 5:
        return True

    if weekday == 6:
        return local_time < open_time

    if weekday == 4 and local_time >= close_time:
        return True

    if close_time < open_time:
        if close_time <= local_time < open_time:
            return True

    return False


# ==========================================
# CANDLE GAP INSPECTION
# ==========================================

def _inspect_candle_gaps(
    candles,
    timeframe,
    source,
    symbol=None,
):
    if len(candles) < 2:
        return []

    duration_ms = TIMEFRAME_SETTINGS[
        timeframe
    ]["duration_ms"]

    gaps = []

    for previous, current in zip(
        candles,
        candles[1:],
    ):
        difference = (
            current.timestamp
            - previous.timestamp
        )

        if difference <= 0:
            raise RuntimeError(
                f"{source} {timeframe}: "
                "timestamp tartibi noto'g'ri"
            )

        if difference <= duration_ms:
            continue

        missing = max(
            0,
            round(difference / duration_ms) - 1,
        )

        if source == "binance_spot":
            gaps.append({
                "from": previous.timestamp,
                "to": current.timestamp,
                "missing_intervals": missing,
            })
            continue

        missing_timestamps = list(range(
            previous.timestamp + duration_ms,
            current.timestamp,
            duration_ms,
        ))

        expected_closure = (
            source == "dukascopy"
            and symbol is not None
            and bool(missing_timestamps)
            and all(
                _is_expected_market_closure(
                    timestamp,
                    symbol,
                )
                for timestamp in missing_timestamps
            )
        )

        if expected_closure:
            continue

        gaps.append({
            "from": previous.timestamp,
            "to": current.timestamp,
            "missing_intervals": missing,
        })

    if gaps:
        warnings.warn(
            f"{source} {timeframe}: "
            f"{len(gaps)} ta ehtimoliy sham uzilishi. "
            f"Birinchi uzilishlar: {gaps[:5]}",
            RuntimeWarning,
        )

    return gaps


# ==========================================
# NORMALIZE CANDLES
# ==========================================

def _build_candles(
    raw_candles,
    timeframe,
    source,
    limit,
    symbol=None,
):
    duration_ms = TIMEFRAME_SETTINGS[
        timeframe
    ]["duration_ms"]

    now_ms = int(
        datetime.now(timezone.utc).timestamp() * 1000
    )

    candles_by_time = {}

    for item in raw_candles:
        try:
            timestamp = int(item["timestamp"])

            if not _valid_timestamp(timestamp):
                continue

            # Faqat to'liq yopilgan shamlar.
            if timestamp + duration_ms > now_ms:
                continue

            open_price = float(item["open"])
            high = float(item["high"])
            low = float(item["low"])
            close = float(item["close"])
            volume = float(item["volume"])

            if not _valid_prices(
                open_price,
                high,
                low,
                close,
            ):
                continue

            if not _valid_volume(volume):
                continue

            candle = Candle(
                timestamp=timestamp,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                is_closed=True,
                source=source,
            )

            if timestamp in candles_by_time:
                previous = candles_by_time[timestamp]

                if previous != candle:
                    raise RuntimeError(
                        f"{source} {timeframe}: "
                        f"qarama-qarshi duplicate: {timestamp}"
                    )

                continue

            candles_by_time[timestamp] = candle

        except RuntimeError:
            raise

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

    _inspect_candle_gaps(
        candles=candles,
        timeframe=timeframe,
        source=source,
        symbol=symbol,
    )

    result = candles[-limit:]

    if len(result) < MIN_CANDLES:
        raise RuntimeError(
            f"{source} {timeframe}: "
            "yakuniy shamlar soni yetarli emas"
        )

    if not all(
        candle.is_closed
        for candle in result
    ):
        raise RuntimeError(
            f"{source} {timeframe}: "
            "yopilmagan sham aniqlandi"
        )

    return result


# ==========================================
# BINANCE DATA
# PAGINATION: 1000 CANDLES PER REQUEST
# ==========================================

def _parse_binance_rows(rows):
    parsed = []

    for item in rows:
        if not isinstance(item, (list, tuple)):
            continue

        if len(item) < 6:
            continue

        try:
            parsed.append({
                "timestamp": int(item[0]),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
            })

        except (
            TypeError,
            ValueError,
            OverflowError,
        ):
            continue

    return parsed


def _fetch_binance(symbol, timeframe, limit):
    binance_symbol = BINANCE_SYMBOLS.get(symbol)

    if not binance_symbol:
        raise ValueError(
            f"Binance instrument kodi topilmadi: {symbol}"
        )

    settings = TIMEFRAME_SETTINGS[timeframe]

    duration_ms = settings["duration_ms"]

    now_ms = int(
        datetime.now(timezone.utc).timestamp() * 1000
    )

    end_time = now_ms - duration_ms

    remaining = limit
    raw_candles = []

    previous_oldest = None

    while remaining > 0:
        page_limit = min(
            BINANCE_MAX_PAGE,
            remaining,
        )

        try:
            response = session.get(
                BINANCE_URL,
                params={
                    "symbol": binance_symbol,
                    "interval": settings["interval"],
                    "limit": page_limit,
                    "endTime": end_time,
                },
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()
            raw_data = response.json()

        except requests.RequestException as error:
            raise RuntimeError(
                f"Binance ulanish xatosi: {error}"
            ) from error

        except ValueError as error:
            raise RuntimeError(
                "Binance JSON javobini o'qib bo'lmadi"
            ) from error

        if not isinstance(raw_data, list):
            raise RuntimeError(
                "Binance noto'g'ri ma'lumot qaytardi"
            )

        if not raw_data:
            break

        parsed = _parse_binance_rows(raw_data)

        if not parsed:
            break

        raw_candles.extend(parsed)

        oldest_timestamp = min(
            item["timestamp"]
            for item in parsed
        )

        if (
            previous_oldest is not None
            and oldest_timestamp >= previous_oldest
        ):
            raise RuntimeError(
                "Binance pagination oldinga siljimadi"
            )

        previous_oldest = oldest_timestamp

        remaining = limit - len({
            item["timestamp"]
            for item in raw_candles
        })

        if len(parsed) < page_limit:
            break

        end_time = oldest_timestamp - 1

        if end_time <= 0:
            break

    return _build_candles(
        raw_candles=raw_candles,
        timeframe=timeframe,
        source="binance_spot",
        limit=limit,
        symbol=symbol,
    )


# ==========================================
# DUKASCOPY DATA
# ==========================================

def _fetch_dukascopy(symbol, timeframe, limit):
    instrument = DUKASCOPY_SYMBOLS.get(symbol)

    if not instrument:
        raise ValueError(
            f"Dukascopy instrument kodi topilmadi: {symbol}"
        )

    settings = TIMEFRAME_SETTINGS[timeframe]

    now = datetime.now(timezone.utc)

    lookback_days = _calculate_lookback_days(
        timeframe,
        limit,
    )

    from_date = (
        now - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")

    to_date = now.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

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
            "-i", instrument,
            "-from", from_date,
            "-to", to_date,
            "-t", settings["dukascopy_tf"],
            "-f", "json",
            "-v",
            "-dir", output_dir,
            "-fn", safe_name,
            "--cache",
            "--retries", "3",
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=DUKASCOPY_TIMEOUT,
                check=False,
            )

        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"Dukascopy {symbol} {timeframe}: "
                "so'rov vaqti tugadi"
            ) from error

        except OSError as error:
            raise RuntimeError(
                "Dukascopy dasturini ishga tushirib bo'lmadi"
            ) from error

        if result.returncode != 0:
            diagnostic = "\n".join(
                part
                for part in (
                    result.stdout,
                    result.stderr,
                )
                if part and part.strip()
            )

            raise RuntimeError(
                "Dukascopy ma'lumot olishda xato: "
                + (
                    diagnostic[-2000:]
                    or "Noma'lum xato"
                )
            )

        output_file = os.path.join(
            output_dir,
            safe_name + ".json",
        )

        if not os.path.isfile(output_file):
            raise RuntimeError(
                "Dukascopy JSON fayli yaratilmadi"
            )

        try:
            with open(
                output_file,
                "r",
                encoding="utf-8",
            ) as file:
                raw_data = json.load(file)

        except (
            OSError,
            json.JSONDecodeError,
        ) as error:
            raise RuntimeError(
                "Dukascopy JSON faylini o'qib bo'lmadi"
            ) from error

    if not isinstance(raw_data, list):
        raise RuntimeError(
            "Dukascopy noto'g'ri JSON format qaytardi"
        )

    raw_candles = []

    for item in raw_data:
        if not isinstance(item, dict):
            continue

        try:
            raw_candles.append({
                "timestamp": int(item["timestamp"]),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item["volume"]),
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
        symbol=symbol,
    )


# ==========================================
# PUBLIC DATA API
# ==========================================

def get_candles(
    symbol,
    timeframe,
    limit=1000,
):
    """
    Belgilangan instrument va timeframe uchun
    yopilgan Candle obyektlarini qaytaradi.

    limit: 220 dan 7000 gacha.
    """

    _validate_request(
        symbol,
        timeframe,
        limit,
    )

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


def get_market_data(
    symbol,
    timeframe,
    limit=1000,
):
    """
    V6 tahlil modullari uchun MarketData qaytaradi.
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
