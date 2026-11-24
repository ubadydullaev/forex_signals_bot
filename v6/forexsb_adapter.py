
import gzip
import json
import logging
import struct
from datetime import datetime, timedelta, timezone, time as dt_time
from zoneinfo import ZoneInfo

import requests


logger = logging.getLogger("V6.FOREXSB")

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
DIAGNOSTIC_LIMIT = 250
MAX_DATA_AGE_HOURS = 72

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 ForexSignalsBot-V6",
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
            raise RuntimeError("ForexSB gzip faylini ochib bo'lmadi") from error

    return content


def _get_metadata():
    global _metadata_cache

    if _metadata_cache is not None:
        return _metadata_cache

    content = _request_content(METADATA_URL, timeout=30)

    try:
        metadata = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("ForexSB metadata JSON formatida emas") from error

    if not isinstance(metadata, dict):
        raise RuntimeError("ForexSB metadata formati noto'g'ri")

    _metadata_cache = metadata
    return metadata


def _find_symbol_metadata(metadata, symbol):
    target = symbol.lower()

    def search(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() == target and isinstance(child, dict):
                    return child

            for child in value.values():
                found = search(child)
                if found is not None:
                    return found

        elif isinstance(value, list):
            for child in value:
                found = search(child)
                if found is not None:
                    return found

        return None

    result = search(metadata)
    if result is None:
        raise RuntimeError(f"ForexSB metadata ichida {symbol} topilmadi")

    return result


def _get_price_scale(symbol):
    item = _find_symbol_metadata(_get_metadata(), symbol)

    candidates = [
        item.get("Scale"),
        item.get("scale"),
        item.get("PriceScale"),
        item.get("priceScale"),
        item.get("Digits"),
        item.get("digits"),
    ]

    for value in candidates:
        if value is None:
            continue

        try:
            number = float(value)
        except (TypeError, ValueError):
            continue

        if number > 0:
            return 10 ** int(number) if number <= 10 else number

    raise RuntimeError(f"{symbol} uchun ForexSB price scale topilmadi")


def _looks_like_error_response(raw):
    sample = raw[:512].lstrip().lower()
    return sample.startswith((
        b"<!doctype html",
        b"<html",
        b"<?xml",
        b"{",
        b"[",
    ))


def _get_records(symbol, minutes):
    cache_key = (symbol, minutes)

    if cache_key in _data_cache:
        return _data_cache[cache_key]

    filename = f"{symbol}{minutes}.lb.gz"
    url = f"{DATA_URL}/{filename}"

    try:
        raw = _request_content(url, timeout=90)
    except requests.HTTPError as error:
        status = getattr(error.response, "status_code", None)
        if status == 404:
            raise RuntimeError(f"ForexSB fayli topilmadi: {filename}") from error
        raise RuntimeError(f"ForexSB HTTP xatosi {filename}: {error}") from error

    if _looks_like_error_response(raw):
        preview = raw[:180].decode("utf-8", errors="replace")
        raise RuntimeError(
            f"ForexSB binary o'rniga xato matn qaytardi: {preview}"
        )

    if not raw or len(raw) % RECORD_SIZE != 0:
        raise RuntimeError(
            f"ForexSB {filename}: binary hajmi noto'g'ri ({len(raw)} byte)"
        )

    try:
        records = list(struct.iter_unpack("<7i", raw))
    except struct.error as error:
        raise RuntimeError(f"ForexSB {filename}: binary parsing xatosi") from error

    if not records:
        raise RuntimeError(f"ForexSB {filename}: yozuvlar topilmadi")

    _data_cache[cache_key] = records
    return records


def _records_to_candles(records, price_scale):
    unique = {}

    for record in records:
        timestamp_minutes, open_, high, low, close, volume, spread = record
        timestamp = EPOCH + timedelta(minutes=timestamp_minutes)

        candle = {
            "timestamp": int(timestamp.timestamp() * 1000),
            "open": open_ / price_scale,
            "high": high / price_scale,
            "low": low / price_scale,
            "close": close / price_scale,
            "volume": max(0, volume),
            "spread": spread,
        }

        if not (
            candle["open"] > 0
            and candle["high"] >= candle["low"]
            and candle["high"] >= max(candle["open"], candle["close"])
            and candle["low"] <= min(candle["open"], candle["close"])
        ):
            continue

        unique[candle["timestamp"]] = candle

    return sorted(unique.values(), key=lambda item: item["timestamp"])


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


def _nth_weekday(year, month, weekday, n):
    first = datetime(year, month, 1).date()
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (n - 1) * 7)


def _last_weekday(year, month, weekday):
    if month == 12:
        last = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else:
        last = datetime(year, month + 1, 1).date() - timedelta(days=1)

    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed_date(day):
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _us_market_holidays(year):
    holidays = {
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _last_weekday(year, 5, 0),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
    }

    for month, day in [(1, 1), (6, 19), (7, 4), (12, 25)]:
        holidays.add(_observed_date(datetime(year, month, day).date()))

    thanksgiving = _nth_weekday(year, 11, 3, 4)
    holidays.add(thanksgiving + timedelta(days=1))

    holidays.add(datetime(year, 12, 24).date())
    holidays.add(datetime(year, 12, 31).date())

    good_friday = _easter_sunday(year) - timedelta(days=2)
    holidays.add(good_friday)
    holidays.add(good_friday - timedelta(days=1))

    return holidays


def _expected_closure(timestamp, symbol, timeframe):
    dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
    market_date = dt.date()

    holidays = (
        _us_market_holidays(market_date.year)
        | _us_market_holidays(market_date.year - 1)
        | _us_market_holidays(market_date.year + 1)
    )

    if timeframe == "D1":
        return market_date.weekday() >= 5 or market_date in holidays

    market_tz = ZoneInfo("America/New_York")
    local = dt.astimezone(market_tz)
    weekday = local.weekday()
    clock = local.time().replace(tzinfo=None)

    if market_date in holidays:
        return True

    close_time = dt_time(17, 0)
    open_time = (
        dt_time(18, 0)
        if symbol in ("XAUUSD", "XAGUSD")
        else dt_time(17, 0)
    )

    if weekday == 5:
        return True

    if weekday == 6 and clock < open_time:
        return True

    if weekday == 4 and clock >= close_time:
        return True

    if symbol in ("XAUUSD", "XAGUSD"):
        if weekday < 5 and close_time <= clock < open_time:
            return True

    return False


def _aggregate_candles(candles, timeframe, symbol):
    if timeframe in ("M15", "M30"):
        return candles

    if timeframe not in ("H1", "H4", "D1"):
        raise ValueError(f"ForexSB timeframe qo'llab-quvvatlanmaydi: {timeframe}")

    target_minutes = TIMEFRAME_MINUTES[timeframe]
    target_ms = target_minutes * 60 * 1000
    base_ms = 30 * 60 * 1000

    buckets = {}

    for candle in candles:
        bucket_start = (candle["timestamp"] // target_ms) * target_ms
        buckets.setdefault(bucket_start, []).append(candle)

    result = []

    for bucket_start, items in sorted(buckets.items()):
        items.sort(key=lambda item: item["timestamp"])
        timestamps = [item["timestamp"] for item in items]

        if len(timestamps) != len(set(timestamps)):
            continue

        if timeframe == "H1":
            expected = [
                bucket_start,
                bucket_start + base_ms,
            ]

            if timestamps != expected:
                continue

        elif timeframe == "H4":
            expected = [
                bucket_start + i * base_ms
                for i in range(8)
            ]
            present = set(timestamps)
            missing = [ts for ts in expected if ts not in present]

            # Qisqa juma sessiyasida 2 ta M30 sham ham qabul qilinadi.
            if missing:
                if len(items) < 2:
                    continue

                if not all(
                    _expected_closure(ts, symbol, "M30")
                    for ts in missing
                ):
                    continue

        else:
            expected = [
                bucket_start + i * base_ms
                for i in range(48)
            ]
            present = set(timestamps)
            missing = [ts for ts in expected if ts not in present]

            # D1 qisqa bayram sessiyasi uchun minimal talab.
            if len(items) < 24:
                continue

            if timestamps[0] > bucket_start + 4 * 60 * 60 * 1000:
                continue

            if timestamps[-1] < bucket_start + 17 * 60 * 60 * 1000:
                continue

            # Ichki yo'qolgan shamlar faqat bozor yopilgan vaqtda ruxsat.
            # Oxirgi shamdan keyingi yopilish vaqti qisqa sessiya bo'lishi mumkin.
            last_timestamp = timestamps[-1]
            internal_missing = [
                ts for ts in missing if ts < last_timestamp
            ]

            if internal_missing and not all(
                _expected_closure(ts, symbol, "M30")
                for ts in internal_missing
            ):
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



def _diagnose_recent_candles(candles, symbol, timeframe):
    duration_ms = TIMEFRAME_MINUTES[timeframe] * 60 * 1000
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    closed = [
        candle for candle in candles
        if int(candle["timestamp"]) + duration_ms <= now_ms
    ]
    closed.sort(key=lambda item: int(item["timestamp"]))
    recent = closed[-DIAGNOSTIC_LIMIT:]

    if len(recent) < DIAGNOSTIC_LIMIT:
        raise RuntimeError(
            f"{symbol} {timeframe}: yopilgan shamlar yetarli emas "
            f"({len(recent)}/{DIAGNOSTIC_LIMIT})"
        )

    all_gaps = []

    for previous, current in zip(recent, recent[1:]):
        previous_ts = int(previous["timestamp"])
        current_ts = int(current["timestamp"])

        if current_ts <= previous_ts:
            raise RuntimeError(
                f"{symbol} {timeframe}: timestamp tartibi noto'g'ri"
            )

        missing = range(
            previous_ts + duration_ms,
            current_ts,
            duration_ms,
        )

        unexpected = [
            ts for ts in missing
            if not _expected_closure(ts, symbol, timeframe)
        ]

        if unexpected:
            all_gaps.append({
                "from": datetime.fromtimestamp(
                    previous_ts / 1000, timezone.utc
                ).isoformat(),
                "to": datetime.fromtimestamp(
                    current_ts / 1000, timezone.utc
                ).isoformat(),
                "missing": len(unexpected),
                "age_days": (now_ms - current_ts) / 86400000,
            })

    tolerated_gaps = []
    gaps = list(all_gaps)

    # Faqat bitta eski D1 uzilishiga ruxsat.
    # Sham yaratilmaydi yoki narxlar to'ldirilmaydi.
    if timeframe == "D1":
        eligible = [
            gap for gap in all_gaps
            if gap["missing"] == 1
            and gap["age_days"] > 30
        ]

        if len(eligible) == 1:
            tolerated_gaps = eligible
            gaps = [
                gap for gap in all_gaps
                if gap not in tolerated_gaps
            ]

    first_ts = int(recent[0]["timestamp"])
    last_ts = int(recent[-1]["timestamp"])
    age_hours = (now_ms - (last_ts + duration_ms)) / 3600000

    first_text = datetime.fromtimestamp(
        first_ts / 1000, timezone.utc
    ).isoformat()
    last_text = datetime.fromtimestamp(
        last_ts / 1000, timezone.utc
    ).isoformat()

    if age_hours > MAX_DATA_AGE_HOURS:
        status = "FAIL_STALE_DATA"
    elif gaps:
        status = "FAIL_RECENT_GAPS"
    elif tolerated_gaps:
        status = "PASS_WITH_1_OLD_D1_GAP"
    else:
        status = "PASS"

    logger.info(
        "[FOREXSB QUALITY] %s %s | count=%s | first=%s | last=%s | "
        "age_hours=%.2f | gaps=%s | tolerated=%s | status=%s",
        symbol,
        timeframe,
        len(recent),
        first_text,
        last_text,
        age_hours,
        len(gaps),
        len(tolerated_gaps),
        status,
    )

    for gap in tolerated_gaps:
        logger.warning(
            "[FOREXSB TOLERATED OLD D1 GAP] %s %s | %s",
            symbol,
            timeframe,
            gap,
        )

    for gap in gaps[:10]:
        logger.warning(
            "[FOREXSB RECENT GAP] %s %s | %s",
            symbol,
            timeframe,
            gap,
        )

    if status.startswith("FAIL"):
        raise RuntimeError(
            f"{symbol} {timeframe}: ForexSB sifati tekshiruvdan o'tmadi: "
            f"{status}; gaps={len(gaps)}; age_hours={age_hours:.2f}"
        )

    return status


def get_forexsb_raw_candles(symbol, timeframe):
    symbol = symbol.upper()
    timeframe = timeframe.upper()

    if timeframe not in TIMEFRAME_MINUTES:
        raise ValueError(f"ForexSB timeframe noto'g'ri: {timeframe}")

    if timeframe in ("M15", "M30"):
        records = _get_records(symbol, TIMEFRAME_MINUTES[timeframe])
    else:
        records = _get_records(symbol, 30)

    scale = _get_price_scale(symbol)
    base_candles = _records_to_candles(records, scale)

    if not base_candles:
        raise RuntimeError(f"ForexSB {symbol}: yaroqli shamlar topilmadi")

    candles = _aggregate_candles(base_candles, timeframe, symbol)

    if not candles:
        raise RuntimeError(
            f"ForexSB {symbol} {timeframe}: agregatsiyadan sham chiqmadi"
        )

    _diagnose_recent_candles(candles, symbol, timeframe)
    return candles
