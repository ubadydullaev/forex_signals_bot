
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

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/gzip, application/octet-stream, */*",
})

_metadata_cache = None
_data_cache = {}


def _decode_response(response, filename):
    response.raise_for_status()

    content = response.content

    if not content:
        raise RuntimeError(
            f"ForexSB bo'sh javob qaytardi: {filename}"
        )

    # requests gzip Content-Encoding javobini avtomatik ochishi mumkin.
    # Shuning uchun avval gzip magic bytes tekshiriladi.
    if content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except (OSError, EOFError) as error:
            raise RuntimeError(
                f"ForexSB gzip buzilgan: {filename}"
            ) from error

    # HTML yoki JSON xato sahifalarini binary deb qabul qilmaymiz.
    if content.lstrip().startswith((b"<", b"{", b"[")):
        preview = content[:180].decode(
            "utf-8", errors="replace"
        )
        raise RuntimeError(
            f"ForexSB binary o'rniga matn qaytardi: "
            f"{filename}: {preview}"
        )

    return content


def _get_metadata():
    global _metadata_cache

    if _metadata_cache is not None:
        return _metadata_cache

    response = session.get(
        METADATA_URL,
        timeout=30,
    )

    content = _decode_response(
        response,
        "premium.json.gz",
    )

    try:
        metadata = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "ForexSB metadata JSON formati noto'g'ri"
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

    try:
        scale = float(result.get("priceScale"))
    except (TypeError, ValueError):
        raise RuntimeError(
            f"ForexSB {symbol}: priceScale aniqlanmadi"
        )

    if scale <= 0:
        raise RuntimeError(
            f"ForexSB {symbol}: priceScale noto'g'ri"
        )

    return scale


def _download_native(symbol, minutes):
    cache_key = (symbol, minutes)

    if cache_key in _data_cache:
        return _data_cache[cache_key]

    filename = f"{symbol}{minutes}.lb.gz"
    url = f"{DATA_URL}/{filename}"

    response = session.get(
        url,
        timeout=90,
    )

    if response.status_code == 404:
        raise RuntimeError(
            f"ForexSB fayli topilmadi: {filename}"
        )

    raw = _decode_response(response, filename)

    record_size = 28

    if not raw or len(raw) % record_size != 0:
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

    _data_cache[cache_key] = records
    return records


def _records_to_candles(records, price_scale):
    epoch = datetime(
        2000, 1, 1,
        tzinfo=timezone.utc,
    )

    result = []

    for record in records:
        (
            minute,
            open_value,
            high_value,
            low_value,
            close_value,
            volume,
            spread,
        ) = record

        if minute < 0:
            continue

        timestamp = int(
            (
                epoch + timedelta(minutes=minute)
            ).timestamp() * 1000
        )

        open_price = open_value / price_scale
        high_price = high_value / price_scale
        low_price = low_value / price_scale
        close_price = close_value / price_scale

        if (
            low_price <= 0
            or high_price < low_price
            or not (
                low_price <= open_price <= high_price
                and low_price <= close_price <= high_price
            )
        ):
            continue

        result.append({
            "timestamp": timestamp,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": max(0, volume),
        })

    result.sort(
        key=lambda item: item["timestamp"]
    )

    return result


def _aggregate_candles(candles, timeframe):
    target_minutes = TIMEFRAME_MINUTES[timeframe]
    source_minutes = 30
    required = target_minutes // source_minutes

    if required <= 1:
        return candles

    interval_ms = target_minutes * 60 * 1000
    source_ms = source_minutes * 60 * 1000

    buckets = {}

    for candle in candles:
        timestamp = candle["timestamp"]

        bucket_start = (
            timestamp // interval_ms
        ) * interval_ms

        buckets.setdefault(
            bucket_start, []
        ).append(candle)

    result = []

    for bucket_start, items in sorted(buckets.items()):
        items.sort(
            key=lambda item: item["timestamp"]
        )

        if len(items) != required:
            continue

        expected_timestamps = [
            bucket_start + index * source_ms
            for index in range(required)
        ]

        actual_timestamps = [
            item["timestamp"]
            for item in items
        ]

        if actual_timestamps != expected_timestamps:
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
    if timeframe not in TIMEFRAME_MINUTES:
        raise ValueError(
            f"ForexSB timeframe qo'llab-quvvatlanmaydi: {timeframe}"
        )

    metadata = _get_metadata()
    price_scale = _find_symbol_metadata(
        metadata,
        symbol,
    )

    if timeframe == "M15":
        records = _download_native(symbol, 15)

        return _records_to_candles(
            records,
            price_scale,
        )

    records = _download_native(symbol, 30)

    candles = _records_to_candles(
        records,
        price_scale,
    )

    if timeframe == "M30":
        return candles

    return _aggregate_candles(
        candles,
        timeframe,
    )
