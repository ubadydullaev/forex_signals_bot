
import json
import math
import os
import re
import subprocess
import tempfile
import time
import warnings
from datetime import datetime, timedelta, timezone, time as dt_time
from zoneinfo import ZoneInfo

import requests

from .models import Candle, MarketData
from .config import SYMBOLS, TIMEFRAMES
from .lse_adapter import get_lse_candles


BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"
TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"

MIN_CANDLES = 220
MAX_CANDLES = 10000
BINANCE_MAX_PAGE = 1000

REQUEST_TIMEOUT = 30
DUKASCOPY_TIMEOUT = 240
MAX_STALE_INTERVALS = 2

CACHE_FILE = os.path.join(
    os.path.dirname(__file__),
    ".market_data_cache.json",
)

TWELVE_DATA_SYMBOLS = {
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "XAUUSD": "XAU/USD",
    "XAGUSD": "XAG/USD",
}

DUKASCOPY_SYMBOLS = {
    "XAUUSD": "xauusd",
    "XAGUSD": "xagusd",
    "EURUSD": "eurusd",
    "GBPUSD": "gbpusd",
}

BINANCE_SYMBOLS = {
    "BTCUSD": "BTCUSDT",
    "ETHUSD": "ETHUSDT",
}

TIMEFRAME_SETTINGS = {
    "M15": {
        "interval": "15m",
        "dukascopy_tf": "m15",
        "twelve_interval": "15min",
        "duration_ms": 15 * 60 * 1000,
    },
    "M30": {
        "interval": "30m",
        "dukascopy_tf": "m30",
        "twelve_interval": "30min",
        "duration_ms": 30 * 60 * 1000,
    },
    "H1": {
        "interval": "1h",
        "dukascopy_tf": "h1",
        "twelve_interval": "1h",
        "duration_ms": 60 * 60 * 1000,
    },
    "H4": {
        "interval": "4h",
        "dukascopy_tf": "h4",
        "twelve_interval": "4h",
        "duration_ms": 4 * 60 * 60 * 1000,
    },
    "D1": {
        "interval": "1d",
        "dukascopy_tf": "d1",
        "twelve_interval": "1day",
        "duration_ms": 24 * 60 * 60 * 1000,
    },
}

session = requests.Session()
session.headers.update({
    "User-Agent": "ForexSignalsBot-V6/6.0"
})

_last_twelve_request = 0.0
TWELVE_REQUEST_INTERVAL = 8.5


# =========================================================
# VALIDATION
# =========================================================

def _validate_request(symbol, timeframe, limit):
    if symbol not in SYMBOLS:
        raise ValueError(f"V6 uchun noma'lum instrument: {symbol}")

    if timeframe not in TIMEFRAMES:
        raise ValueError(f"V6 uchun noma'lum timeframe: {timeframe}")

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit musbat butun son bo'lishi kerak")

    if not MIN_CANDLES <= limit <= MAX_CANDLES:
        raise ValueError(
            f"Shamlar soni {MIN_CANDLES}-{MAX_CANDLES} oralig'ida bo'lishi kerak"
        )


def _valid_prices(open_price, high, low, close):
    values = (open_price, high, low, close)

    if not all(math.isfinite(v) and v > 0 for v in values):
        return False

    return (
        high >= low
        and low <= open_price <= high
        and low <= close <= high
    )


def _valid_timestamp(timestamp):
    return (
        not isinstance(timestamp, bool)
        and isinstance(timestamp, int)
        and timestamp >= 100_000_000_000
    )


def _calculate_lookback_days(timeframe, limit):
    seconds = TIMEFRAME_SETTINGS[timeframe]["duration_ms"] / 1000
    days = limit * seconds / 86400 * (7 / 5) * 1.35
    return max(7, math.ceil(days) + 2)


# =========================================================
# MARKET CLOSURES
# =========================================================

def _easter_sunday(year):
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime(year, month, day).date()


def _is_expected_market_closure(timestamp, symbol, timeframe=None):
    if symbol not in DUKASCOPY_SYMBOLS:
        return False

    local_dt = datetime.fromtimestamp(
        timestamp / 1000,
        tz=timezone.utc,
    )

    if timeframe == "D1":
        market_date = local_dt.date()

        if market_date.weekday() >= 5:
            return True

        good_friday = _easter_sunday(market_date.year) - timedelta(days=2)
        return market_date == good_friday

    if symbol in ("XAUUSD", "XAGUSD"):
        market_tz = ZoneInfo("America/New_York")
        close_time = dt_time(17, 0)
        open_time = dt_time(18, 0)
    else:
        market_tz = ZoneInfo("America/New_York")
        close_time = dt_time(17, 0)
        open_time = dt_time(17, 0)

    local_dt = local_dt.astimezone(market_tz)
    weekday = local_dt.weekday()
    local_time = local_dt.time().replace(tzinfo=None)

    if weekday == 5:
        return True

    if weekday == 6:
        return local_time < open_time

    if weekday == 4 and local_time >= close_time:
        return True

    if close_time < open_time and close_time <= local_time < open_time:
        return True

    return False


# =========================================================
# CANDLE NORMALIZATION AND GAP CHECK
# =========================================================

def _inspect_candle_gaps(candles, timeframe, source, symbol=None):
    if len(candles) < 2:
        return []

    duration_ms = TIMEFRAME_SETTINGS[timeframe]["duration_ms"]
    gaps = []

    for previous, current in zip(candles, candles[1:]):
        difference = current.timestamp - previous.timestamp

        if difference <= 0:
            raise RuntimeError(
                f"{source} {timeframe}: timestamp tartibi noto'g'ri"
            )

        if difference <= duration_ms:
            continue

        missing = max(0, round(difference / duration_ms) - 1)

        if source == "binance_spot":
            gaps.append({
                "from": previous.timestamp,
                "to": current.timestamp,
                "missing_intervals": missing,
            })
            continue

        missing_timestamps = range(
            previous.timestamp + duration_ms,
            current.timestamp,
            duration_ms,
        )

        expected_closure = (
            source in ("dukascopy", "lse", "twelve_data")
            and symbol is not None
            and bool(missing)
            and all(
                _is_expected_market_closure(ts, symbol, timeframe)
                for ts in missing_timestamps
            )
        )

        if not expected_closure:
            gaps.append({
                "from": previous.timestamp,
                "to": current.timestamp,
                "missing_intervals": missing,
            })

    if gaps:
        warnings.warn(
            f"{source} {timeframe}: {len(gaps)} ta ehtimoliy sham uzilishi. "
            f"Birinchi uzilishlar: {gaps[:5]}",
            RuntimeWarning,
        )

    return gaps


def _build_candles(raw_candles, timeframe, source, limit, symbol=None):
    duration_ms = TIMEFRAME_SETTINGS[timeframe]["duration_ms"]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candles_by_time = {}

    for item in raw_candles:
        try:
            timestamp = int(item["timestamp"])

            if not _valid_timestamp(timestamp):
                continue

            if timestamp + duration_ms > now_ms:
                continue

            open_price = float(item["open"])
            high = float(item["high"])
            low = float(item["low"])
            close = float(item["close"])
            volume = float(item.get("volume", 0) or 0)

            if not _valid_prices(open_price, high, low, close):
                continue

            if not math.isfinite(volume) or volume < 0:
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

            existing = candles_by_time.get(timestamp)

            if existing is not None and existing != candle:
                raise RuntimeError(
                    f"{source} {timeframe}: qarama-qarshi duplicate: {timestamp}"
                )

            candles_by_time[timestamp] = candle

        except RuntimeError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    candles = sorted(
        candles_by_time.values(),
        key=lambda candle: candle.timestamp,
    )

    if len(candles) < MIN_CANDLES:
        raise RuntimeError(
            f"{source} {timeframe}: yopilgan shamlar yetarli emas: "
            f"{len(candles)}/{MIN_CANDLES}"
        )

    _inspect_candle_gaps(candles, timeframe, source, symbol)

    result = candles[-limit:]

    if len(result) < MIN_CANDLES:
        raise RuntimeError(
            f"{source} {timeframe}: yakuniy shamlar soni yetarli emas"
        )

    if not all(candle.is_closed for candle in result):
        raise RuntimeError(
            f"{source} {timeframe}: yopilmagan sham aniqlandi"
        )

    return result


# =========================================================
# TWELVE DATA CACHE
# =========================================================

def _load_cache():
    if not os.path.isfile(CACHE_FILE):
        return {}

    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    temporary_file = CACHE_FILE + ".tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(cache, file, separators=(",", ":"))

    os.replace(temporary_file, CACHE_FILE)


def _cache_key(symbol, timeframe):
    return f"{symbol}_{timeframe}"


def _candles_to_cache(candles):
    return [
        {
            "timestamp": c.timestamp,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
        }
        for c in candles
    ]


def _get_cached_twelve_data(symbol, timeframe, limit, allow_stale=False):
    entry = _load_cache().get(_cache_key(symbol, timeframe))

    if not isinstance(entry, dict):
        return None

    try:
        age = time.time() - float(entry["fetched_at"])
        duration_seconds = TIMEFRAME_SETTINGS[timeframe]["duration_ms"] / 1000
        max_age = duration_seconds * (
            MAX_STALE_INTERVALS if allow_stale else 1
        )

        if age < 0 or age > max_age:
            return None

        return _build_candles(
            entry.get("candles", []),
            timeframe,
            "twelve_data",
            limit,
            symbol,
        )

    except (KeyError, TypeError, ValueError, RuntimeError):
        return None


def _store_twelve_data_cache(symbol, timeframe, candles):
    cache = _load_cache()
    cache[_cache_key(symbol, timeframe)] = {
        "fetched_at": time.time(),
        "candles": _candles_to_cache(candles),
    }
    _save_cache(cache)


# =========================================================
# BINANCE
# =========================================================

def _parse_binance_rows(rows):
    parsed = []

    for item in rows:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
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
        except (TypeError, ValueError, OverflowError):
            continue

    return parsed


def _fetch_binance(symbol, timeframe, limit):
    binance_symbol = BINANCE_SYMBOLS.get(symbol)

    if not binance_symbol:
        raise ValueError(f"Binance instrument kodi topilmadi: {symbol}")

    settings = TIMEFRAME_SETTINGS[timeframe]
    duration_ms = settings["duration_ms"]
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000) - duration_ms

    raw_candles = []
    previous_oldest = None

    while len({x["timestamp"] for x in raw_candles}) < limit:
        remaining = limit - len({x["timestamp"] for x in raw_candles})
        page_limit = min(BINANCE_MAX_PAGE, remaining)

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
            raise RuntimeError(f"Binance ulanish xatosi: {error}") from error

        except ValueError as error:
            raise RuntimeError("Binance JSON javobini o'qib bo'lmadi") from error

        if not isinstance(raw_data, list) or not raw_data:
            break

        parsed = _parse_binance_rows(raw_data)

        if not parsed:
            break

        raw_candles.extend(parsed)
        oldest_timestamp = min(x["timestamp"] for x in parsed)

        if previous_oldest is not None and oldest_timestamp >= previous_oldest:
            raise RuntimeError("Binance pagination oldinga siljimadi")

        previous_oldest = oldest_timestamp

        if len(parsed) < page_limit:
            break

        end_time = oldest_timestamp - 1

        if end_time <= 0:
            break

    return _build_candles(
        raw_candles,
        timeframe,
        "binance_spot",
        limit,
        symbol,
    )


# =========================================================
# DUKASCOPY
# =========================================================

def _fetch_dukascopy(symbol, timeframe, limit):
    instrument = DUKASCOPY_SYMBOLS.get(symbol)

    if not instrument:
        raise ValueError(f"Dukascopy instrument kodi topilmadi: {symbol}")

    settings = TIMEFRAME_SETTINGS[timeframe]
    now = datetime.now(timezone.utc)
    lookback_days = _calculate_lookback_days(timeframe, limit)

    from_date = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    safe_name = re.sub(
        r"[^a-zA-Z0-9_]",
        "_",
        f"v6_{instrument}_{timeframe}",
    )

    with tempfile.TemporaryDirectory(prefix="v6_market_") as output_dir:
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
                f"Dukascopy {symbol} {timeframe}: so'rov vaqti tugadi"
            ) from error
        except OSError as error:
            raise RuntimeError(
                "Dukascopy dasturini ishga tushirib bo'lmadi"
            ) from error

        if result.returncode != 0:
            diagnostic = "\n".join(
                part for part in (result.stdout, result.stderr)
                if part and part.strip()
            )
            raise RuntimeError(
                "Dukascopy ma'lumot olishda xato: "
                + (diagnostic[-2000:] or "Noma'lum xato")
            )

        output_file = os.path.join(output_dir, safe_name + ".json")

        if not os.path.isfile(output_file):
            raise RuntimeError("Dukascopy JSON fayli yaratilmadi")

        try:
            with open(output_file, "r", encoding="utf-8") as file:
                raw_data = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "Dukascopy JSON faylini o'qib bo'lmadi"
            ) from error

    if not isinstance(raw_data, list):
        raise RuntimeError("Dukascopy noto'g'ri JSON format qaytardi")

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
                "volume": float(item.get("volume", 0) or 0),
            })
        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    return _build_candles(
        raw_candles,
        timeframe,
        "dukascopy",
        limit,
        symbol,
    )


# =========================================================
# TWELVE DATA
# =========================================================

def _wait_twelve_data_rate_limit():
    global _last_twelve_request

    elapsed = time.monotonic() - _last_twelve_request
    wait_seconds = TWELVE_REQUEST_INTERVAL - elapsed

    if wait_seconds > 0:
        time.sleep(wait_seconds)

    _last_twelve_request = time.monotonic()


def _parse_twelve_datetime(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Twelve Data datetime mavjud emas")

    parsed = datetime.fromisoformat(
        value.strip().replace("Z", "+00:00")
    )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return int(parsed.timestamp() * 1000)


def _fetch_twelve_data(symbol, timeframe, limit):
    api_key = os.getenv("TWELVE_DATA_API_KEY")

    if not api_key:
        raise RuntimeError("TWELVE_DATA_API_KEY GitHub Secret topilmadi")

    twelve_symbol = TWELVE_DATA_SYMBOLS.get(symbol)

    if not twelve_symbol:
        raise ValueError(f"Twelve Data instrumenti mavjud emas: {symbol}")

    _wait_twelve_data_rate_limit()
    settings = TIMEFRAME_SETTINGS[timeframe]

    try:
        response = session.get(
            TWELVE_DATA_URL,
            params={
                "symbol": twelve_symbol,
                "interval": settings["twelve_interval"],
                "outputsize": limit,
                "timezone": "UTC",
                "order": "ASC",
                "apikey": api_key,
            },
            timeout=REQUEST_TIMEOUT,
        )

        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            status_code = (
                response.status_code
                if response is not None
                else "unknown"
            )
            response_body = (
                response.text[:500]
                if response is not None and response.text
                else "Javob matni bo'sh"
            )
            raise RuntimeError(
                f"Twelve Data HTTP {status_code}: {response_body}"
            ) from error

        payload = response.json()

    except requests.RequestException as error:
        raise RuntimeError(
            f"Twelve Data ulanish xatosi: {type(error).__name__}"
        ) from error

    except ValueError as error:
        raise RuntimeError(
            "Twelve Data JSON javobini o'qib bo'lmadi"
        ) from error

    if not isinstance(payload, dict):
        raise RuntimeError("Twelve Data noto'g'ri javob qaytardi")

    if payload.get("status") == "error" or "code" in payload:
        message = payload.get(
            "message",
            payload.get("status", "Noma'lum API xatosi"),
        )
        raise RuntimeError(f"Twelve Data API xatosi: {message}")

    values = payload.get("values")

    if not isinstance(values, list):
        raise RuntimeError("Twelve Data javobida values ro'yxati yo'q")

    raw_candles = []

    for item in values:
        if not isinstance(item, dict):
            continue

        try:
            raw_candles.append({
                "timestamp": _parse_twelve_datetime(item["datetime"]),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item.get("volume", 0) or 0),
            })
        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    candles = _build_candles(
        raw_candles,
        timeframe,
        "twelve_data",
        limit,
        symbol,
    )

    _store_twelve_data_cache(symbol, timeframe, candles)
    return candles


def _fetch_twelve_data_with_cache(symbol, timeframe, limit):
    cached = _get_cached_twelve_data(
        symbol,
        timeframe,
        limit,
        allow_stale=False,
    )

    if cached is not None:
        return cached

    try:
        return _fetch_twelve_data(symbol, timeframe, limit)

    except Exception as api_error:
        stale = _get_cached_twelve_data(
            symbol,
            timeframe,
            limit,
            allow_stale=True,
        )

        if stale is not None:
            warnings.warn(
                f"Twelve Data {symbol} {timeframe} ishlamadi. "
                f"Vaqtinchalik eski kesh ishlatildi: {api_error}",
                RuntimeWarning,
            )
            return stale

        raise


# =========================================================
# LSE
# =========================================================

def _fetch_lse(symbol, timeframe, limit):
    candles = get_lse_candles(
        symbol=symbol,
        timeframe=timeframe,
        limit=min(limit, 250),
    )

    if not isinstance(candles, list):
        raise RuntimeError("LSE noto'g'ri candle ro'yxatini qaytardi")

    normalized = []

    for candle in candles:
        if isinstance(candle, Candle):
            normalized.append({
                "timestamp": candle.timestamp,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            })
        elif isinstance(candle, dict):
            normalized.append(candle)
        else:
            raise RuntimeError("LSE candle formati noto'g'ri")

    return _build_candles(
        normalized,
        timeframe,
        "lse",
        min(limit, 250),
        symbol,
    )


# =========================================================
# PUBLIC DATA API
# =========================================================

def get_candles(symbol, timeframe, limit=1000):
    """
    Forex/metals:
        1. LSE
        2. Dukascopy
        3. Twelve Data

    Crypto:
        Binance
    """

    _validate_request(symbol, timeframe, limit)

    if symbol in BINANCE_SYMBOLS:
        return _fetch_binance(symbol, timeframe, limit)

    if symbol not in DUKASCOPY_SYMBOLS:
        raise ValueError(
            f"{symbol} uchun ma'lumot manbasi aniqlanmagan"
        )

    lse_error = None

    try:
        return _fetch_lse(symbol, timeframe, limit)

    except Exception as error:
        lse_error = error
        warnings.warn(
            f"LSE {symbol} {timeframe} ishlamadi. "
            f"Dukascopy fallback ishga tushadi: {error}",
            RuntimeWarning,
        )

    dukascopy_error = None

    try:
        return _fetch_dukascopy(symbol, timeframe, limit)

    except Exception as error:
        dukascopy_error = error
        warnings.warn(
            f"Dukascopy {symbol} {timeframe} ishlamadi. "
            f"Twelve Data fallback ishga tushadi: {error}",
            RuntimeWarning,
        )

    if symbol not in TWELVE_DATA_SYMBOLS:
        raise RuntimeError(
            f"{symbol} uchun barcha manbalar ishlamadi. "
            f"LSE: {lse_error}. Dukascopy: {dukascopy_error}"
        )

    try:
        return _fetch_twelve_data_with_cache(
            symbol,
            timeframe,
            limit,
        )

    except Exception as twelve_error:
        raise RuntimeError(
            f"{symbol} {timeframe}: barcha manbalar ishlamadi. "
            f"LSE: {lse_error}. "
            f"Dukascopy: {dukascopy_error}. "
            f"Twelve Data: {twelve_error}"
        ) from twelve_error


def get_market_data(symbol, timeframe, limit=1000):
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
