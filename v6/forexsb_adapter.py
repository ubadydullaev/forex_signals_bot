
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
    "User-Agent": "ForexSignalsBot-V6/6.0"
})

_metadata_cache = None
_data_cache = {}


def _get_metadata():
    global _metadata_cache

    if _metadata_cache is not None:
        return _metadata_cache

    response = session.get(METADATA_URL, timeout=30)
    response.raise_for_status()

    try:
        content = gzip.decompress(response.content)
    except OSError:
        content = response.content

    metadata = json.loads(content.decode("utf-8"))

    if not isinstance(metadata, dict):
        raise RuntimeError("ForexSB metadata formati noto'g'ri")

    _metadata_cache = metadata
    return metadata


def _find_symbol_metadata(metadata, symbol):
    symbol_lower = symbol.lower()

    def search(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() == symbol_lower and isinstance(child, dict):
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

    scale = result.get("priceScale")

    try:
        scale = float(scale)
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

    response = session.get(url, timeout=90)

    if response.status_code == 404:
        raise RuntimeError(
            f"ForexSB fayli topilmadi: {filename}"
        )

    response.raise_for_status()

    try:
        raw = gzip.decompress(response.content)
    except OSError as error:
        raise RuntimeError(
            f"ForexSB gzip faylini ochib bo'lmadi: {filename}"
        ) from error

    record_size = 28

    if len(raw) == 0 or len(raw) % record_size != 0:
        raise RuntimeError(
            f"ForexSB {filename}: binary fayl hajmi noto'g'ri"
        )

    records = list(struct.iter_unpack("<7i", raw))

    _data_cache[cache_key] = records
    return records


def _records_to_candles(records, price_scale):
    epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
    result = []

    for record in records:
        minute, open_value, high_value, low_value, close_value, volume, spread = record

        if minute < 0:
            continue

        timestamp = int(
            (epoch + timedelta(minutes=minute)).timestamp() * 1000
        )

        result.append({
            "timestamp": timestamp,
            "open": open_value / price_scale,
            "high": high_value / price_scale,
            "low": low_value / price_scale,
            "close": close_value / price_scale,
            "volume": max(0, volume),
        })

    result.sort(key=lambda item: item["timestamp"])
    return result


def _aggregate_candles(candles, timeframe):
    target_minutes = TIMEFRAME_MINUTES[timeframe]
    source_minutes = 30
    required = target_minutes // source_minutes

    if required <= 1:
        return candles

    buckets = {}

    for candle in candles:
        timestamp = candle["timestamp"]
        bucket_start = (
            timestamp // (target_minutes * 60 * 1000)
        ) * (target_minutes * 60 * 1000)

        buckets.setdefault(bucket_start, []).append(candle)

    result = []

    for bucket_start, items in sorted(buckets.items()):
        items.sort(key=lambda item: item["timestamp"])

        if len(items) != required:
            continue

        expected_timestamps = [
            bucket_start + index * source_minutes * 60 * 1000
            for index in range(required)
        ]

        actual_timestamps = [
            item["timestamp"] for item in items
        ]

        if actual_timestamps != expected_timestamps:
            continue

        result.append({
            "timestamp": bucket_start,
            "open": items[0]["open"],
            "high": max(item["high"] for item in items),
            "low": min(item["low"] for item in items),
            "close": items[-1]["close"],
            "volume": sum(item["volume"] for item in items),
        })

    return result


def get_forexsb_raw_candles(symbol, timeframe):
    if timeframe not in TIMEFRAME_MINUTES:
        raise ValueError(
            f"ForexSB timeframe qo'llab-quvvatlanmaydi: {timeframe}"
        )

    metadata = _get_metadata()
    price_scale = _find_symbol_metadata(metadata, symbol)

    if timeframe == "M15":
        records = _download_native(symbol, 15)
        return _records_to_candles(records, price_scale)

    records = _download_native(symbol, 30)
    candles = _records_to_candles(records, price_scale)

    if timeframe == "M30":
        return candles

    return _aggregate_candles(candles, timeframe)
