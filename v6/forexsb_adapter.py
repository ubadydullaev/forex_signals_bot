
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

    # requests ba'zan gzip Content-Encoding ni avtomatik ochadi.
    # Shuning uchun faqat gzip magic bytes bo'lsa ochamiz.
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

    try:
        raw = _request_content(
            url,
            timeout=90,
        )
    except requests.HTTPError as error:
        status = getattr(
            error.response, "status_code", None
        )
        if status == 404:
            raise RuntimeError(
                f"ForexSB fayli topilmadi: {filename}"
            ) from error
        raise RuntimeError(
            f"ForexSB HTTP xatosi {filename}: {error}"
        ) from error

    if raw.lstrip().startswith((b"<", b"{", b"[")):
        preview = raw[:180].decode(
            "utf-8", errors="replace"
        )
        raise RuntimeError(
            f"ForexSB binary o'rniga matn qaytardi: "
            f"{filename}: {preview}"
        )

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
        bucket_start = (timestamp // interval_ms) * interval_ms

        buckets.setdefault(bucket_start, []).append(candle)

    result = []

    for bucket_start, items in sorted(buckets.items()):
        items.sort(key=lambda item: item["timestamp"])

        actual_timestamps = [
            item["timestamp"] for item in items
        ]

        # D1: Forex bozoridagi kunlik tanaffuslar va
        # ayrim yetishmayotgan M30 shamlariga moslash.
        if timeframe == "D1":
            minimum_bars = 40

            if len(items) < minimum_bars:
                continue

            # Kun boshi va oxiri yetarlicha qamrab olinganmi?
            first_timestamp = actual_timestamps[0]
            last_timestamp = actual_timestamps[-1]

            if first_timestamp > bucket_start + 60 * 60 * 1000:
                continue

            if last_timestamp < bucket_start + 22 * 60 * 60 * 1000:
                continue

            # Bir xil timestamp takrorlangan bo‘lsa, rad etamiz.
            if len(set(actual_timestamps)) != len(actual_timestamps):
                continue

        else:
            # H1 va H4 uchun eski qat'iy tekshiruv saqlanadi.
            if len(items) != required:
                continue

            expected_timestamps = [
                bucket_start + index * source_ms
                for index in range(required)
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
