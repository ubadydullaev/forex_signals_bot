
import gzip
import json
import struct
from datetime import datetime, timedelta, timezone

import requests


BASE_URL = "https://data.forexsb.com/datafeed"
METADATA_URL = f"{BASE_URL}/info/premium.json.gz"
DATA_URL = f"{BASE_URL}/data/dukascopy"

TIMEFRAME_MINUTES = {
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}

RECORD_SIZE = 28
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/gzip, application/json, application/octet-stream, */*",
})

_metadata_cache = None
_data_cache = {}


def _request_content(url, timeout=60):
    response = session.get(url, timeout=timeout)
    response.raise_for_status()

    content = response.content

    if not content:
        raise RuntimeError("ForexSB bo'sh javob qaytardi")

    if content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except (OSError, EOFError) as error:
            raise RuntimeError(
                "ForexSB gzip faylini ochib bo'lmadi"
            ) from error

    return content


def _get_metadata():
    global _metadata_cache

    if _metadata_cache is not None:
        return _metadata_cache

    content = _request_content(
        METADATA_URL,
        timeout=30,
    )

    try:
        metadata = json.loads(
            content.decode("utf-8-sig")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        preview = content[:200].decode(
            "utf-8", errors="replace"
        )
        raise RuntimeError(
            f"ForexSB metadata JSON xatosi: {preview}"
        ) from error

    if not isinstance(metadata, dict):
        raise RuntimeError(
            "ForexSB metadata formati noto'g'ri"
        )

    _metadata_cache = metadata
    return metadata


def _find_symbol_metadata(metadata, symbol):
    symbol_lower = symbol.lower()

    def search(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    str(key).lower() == symbol_lower
                    and isinstance(child, dict)
                ):
                    return child

            for child in value.values():
                result = search(child)
                if result is not None:
                    return result

        elif isinstance(value, list):
            for child in value:
                result = search(child)
                if result is not None:
                    return result

        return None

    result = search(metadata)

    if result is None:
        raise RuntimeError(
            f"ForexSB metadata ichida {symbol} topilmadi"
        )

    return result


def _get_price_scale(symbol):
    metadata = _get_metadata()
    symbol_data = _find_symbol_metadata(
        metadata,
        symbol,
    )

    candidates = [
        symbol_data.get("Scale"),
        symbol_data.get("scale"),
        symbol_data.get("PriceScale"),
        symbol_data.get("priceScale"),
        symbol_data.get("Digits"),
        symbol_data.get("digits"),
    ]

    for value in candidates:
        if value is None:
            continue

        try:
            number = float(value)
        except (TypeError, ValueError):
            continue

        if number > 0:
            if number <= 10:
                return 10 ** int(number)
            return number

    raise RuntimeError(
        f"{symbol} uchun ForexSB price scale topilmadi"
    )


def _looks_like_error_response(raw):
    """
    Faqat aniq HTML yoki JSON xato javoblarini aniqlaydi.
    Binary faylning birinchi baytiga qarab qaror qilmaydi.
    """
    sample = raw[:512].lstrip().lower()

    if sample.startswith((
        b"<!doctype html",
        b"<html",
        b"<?xml",
        b"{",
        b"[",
    )):
        return True

    return False


def _get_records(symbol, minutes):
    cache_key = (symbol, minutes)

    if cache_key in _data_cache:
        return _data_cache[cache_key]

    filename = f"{symbol}{minutes}.lb.gz"
    url = f"{DATA_URL}/{filename}"

    try:
        raw = _request_content(
            url,
            timeout=90,
        )
    except requests.HTTPError as error:
        status = getattr(
            error.response,
            "status_code",
            None,
        )

        if status == 404:
            raise RuntimeError(
                f"ForexSB fayli topilmadi: {filename}"
            ) from error

        raise RuntimeError(
            f"ForexSB HTTP xatosi {filename}: {error}"
        ) from error

    if _looks_like_error_response(raw):
        preview = raw[:180].decode(
            "utf-8",
            errors="replace",
        )
        raise RuntimeError(
            f"ForexSB binary o'rniga xato matn qaytardi: "
            f"{filename}: {preview}"
        )

    if not raw or len(raw) % RECORD_SIZE != 0:
        raise RuntimeError(
            f"ForexSB {filename}: binary hajmi noto'g'ri "
            f"({len(raw)} byte)"
        )

    try:
        records = list(
            struct.iter_unpack("<7i", raw)
        )
    except struct.error as error:
        raise RuntimeError(
            f"ForexSB {filename}: binary parsing xatosi"
        ) from error

    if not records:
        raise RuntimeError(
            f"ForexSB {filename}: yozuvlar topilmadi"
        )

    _data_cache[cache_key] = records
    return records


def _records_to_candles(records, price_scale):
    candles = []

    for record in records:
        timestamp_minutes, open_, high, low, close, volume, spread = record

        timestamp = EPOCH + timedelta(
            minutes=timestamp_minutes
        )

        candle = {
            "timestamp": int(timestamp.timestamp() * 1000),
            "open": open_ / price_scale,
            "high": high / price_scale,
            "low": low / price_scale,
            "close": close / price_scale,
            "volume": max(0, volume),
            "spread": spread,
        }

        if (
            candle["high"] < candle["low"]
            or candle["high"] < max(
                candle["open"],
                candle["close"],
            )
            or candle["low"] > min(
                candle["open"],
                candle["close"],
            )
        ):
            continue

        candles.append(candle)

    candles.sort(
        key=lambda item: item["timestamp"]
    )

    unique = {}
    for candle in candles:
        unique[candle["timestamp"]] = candle

    return list(unique.values())


def _aggregate_candles(candles, timeframe):
    if timeframe == "M15":
        return candles

    if timeframe == "M30":
        return candles

    if timeframe not in ("H1", "H4", "D1"):
        raise ValueError(
            f"ForexSB timeframe qo'llab-quvvatlanmaydi: {timeframe}"
        )

    target_minutes = TIMEFRAME_MINUTES[timeframe]
    target_ms = target_minutes * 60 * 1000
    base_ms = 30 * 60 * 1000

    buckets = {}

    for candle in candles:
        bucket_start = (
            candle["timestamp"] // target_ms
        ) * target_ms

        buckets.setdefault(
            bucket_start, []
        ).append(candle)

    result = []

    for bucket_start, items in sorted(buckets.items()):
        items.sort(
            key=lambda item: item["timestamp"]
        )

        timestamps = [
            item["timestamp"] for item in items
        ]

        if len(timestamps) != len(set(timestamps)):
            continue

        if timeframe == "H1":
            expected_count = 2
        elif timeframe == "H4":
            expected_count = 8
        else:
            expected_count = 48

        # H1 va H4 faqat to'liq shamlar bilan tuziladi.
        if timeframe in ("H1", "H4"):
            if len(items) != expected_count:
                continue

            expected = [
                bucket_start + i * base_ms
                for i in range(expected_count)
            ]

            if timestamps != expected:
                continue

        # D1: ForexSB M30 sessiyasi bo'yicha
        # yetarli qamrov va takrorlanmagan yozuv talab qilinadi.
        if timeframe == "D1":
            minimum_bars = 40

            if len(items) < minimum_bars:
                continue

            if timestamps[0] > bucket_start + 60 * 60 * 1000:
                continue

            if timestamps[-1] < bucket_start + 22 * 60 * 60 * 1000:
                continue

        result.append({
            "timestamp": bucket_start,
            "open": items[0]["open"],
            "high": max(
                item["high"] for item in items
            ),
            "low": min(
                item["low"] for item in items
            ),
            "close": items[-1]["close"],
            "volume": sum(
                item["volume"] for item in items
            ),
        })

    return result


def get_forexsb_raw_candles(symbol, timeframe):
    symbol = symbol.upper()
    timeframe = timeframe.upper()

    if timeframe not in TIMEFRAME_MINUTES:
        raise ValueError(
            f"ForexSB timeframe noto'g'ri: {timeframe}"
        )

    if timeframe in ("M15", "M30"):
        minutes = TIMEFRAME_MINUTES[timeframe]

        records = _get_records(
            symbol,
            minutes,
        )

        scale = _get_price_scale(symbol)

        candles = _records_to_candles(
            records,
            scale,
        )

        if not candles:
            raise RuntimeError(
                f"ForexSB {symbol} {timeframe}: "
                "yaroqli shamlar topilmadi"
            )

        return candles

    records = _get_records(
        symbol,
        30,
    )

    scale = _get_price_scale(symbol)

    base_candles = _records_to_candles(
        records,
        scale,
    )

    candles = _aggregate_candles(
        base_candles,
        timeframe,
    )

    if not candles:
        raise RuntimeError(
            f"ForexSB {symbol} {timeframe}: "
            "agregatsiyadan sham chiqmadi"
        )

    return candles
