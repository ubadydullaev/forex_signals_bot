
import logging
import math
import os

from datetime import datetime, timezone

from .models import Candle


logger = logging.getLogger("V6.LSE")

MIN_CANDLES = 220
MAX_CANDLES = 250
REQUEST_TIMEOUT = 60

TIMEFRAME_MAP = {
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
    "D1": "1d",
}

TIMEFRAME_DURATION_MS = {
    "M15": 15 * 60 * 1000,
    "M30": 30 * 60 * 1000,
    "H1": 60 * 60 * 1000,
    "H4": 4 * 60 * 60 * 1000,
    "D1": 24 * 60 * 60 * 1000,
}

FX_SYMBOLS = {
    "EURUSD": {
        "symbol": "EUR_USD",
        "market": "forex",
    },
    "GBPUSD": {
        "symbol": "GBP_USD",
        "market": "forex",
    },
}

COMMODITY_SEARCH = {
    "XAUUSD": {
        "query": "gold",
        "market": "commodities",
        "preferred": ("XAU", "GOLD"),
    },
    "XAGUSD": {
        "query": "silver",
        "market": "commodities",
        "preferred": ("XAG", "SILVER"),
    },
}


_client = None
_resolved_symbols = {}


def _get_client():
    global _client

    if _client is not None:
        return _client

    api_key = os.getenv("LSE_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError(
            "LSE_API_KEY GitHub Secret topilmadi"
        )

    try:
        from lse import LSE

        _client = LSE(
            api_key=api_key,
            timeout=REQUEST_TIMEOUT,
            retries=2,
            wait_for_limit=True,
        )

    except Exception as error:
        raise RuntimeError(
            f"LSE client ishga tushmadi: {error}"
        ) from error

    return _client


def _parse_timestamp(value):
    if isinstance(value, (int, float)):
        timestamp = int(value)

        # Unix seconds bo'lsa milliseconds ga aylantiramiz.
        if timestamp < 100_000_000_000:
            timestamp *= 1000

        return timestamp

    if not isinstance(value, str) or not value.strip():
        raise ValueError("LSE timestamp mavjud emas")

    parsed = datetime.fromisoformat(
        value.strip().replace("Z", "+00:00")
    )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return int(parsed.timestamp() * 1000)


def _extract_pairs(response):
    if isinstance(response, list):
        return response

    if isinstance(response, dict):
        for key in ("pairs", "results", "data", "items"):
            value = response.get(key)

            if isinstance(value, list):
                return value

    return []


def _resolve_commodity_symbol(client, symbol):
    if symbol in _resolved_symbols:
        return _resolved_symbols[symbol]

    settings = COMMODITY_SEARCH.get(symbol)

    if not settings:
        raise ValueError(
            f"LSE commodity instrumenti noma'lum: {symbol}"
        )

    try:
        response = client.pairs(
            settings["market"],
            q=settings["query"],
            limit=100,
        )

    except Exception as error:
        raise RuntimeError(
            f"LSE {symbol} katalogini olishda xato: {error}"
        ) from error

    pairs = _extract_pairs(response)

    if not pairs:
        raise RuntimeError(
            f"LSE katalogida {symbol} uchun instrument topilmadi"
        )

    preferred = settings["preferred"]
    candidates = []

    for item in pairs:
        if not isinstance(item, dict):
            continue

        pair_symbol = str(
            item.get("symbol", "")
        ).strip()

        name = str(
            item.get("name", "")
        ).strip()

        searchable = (
            pair_symbol.upper() + " " + name.upper()
        )

        if not pair_symbol:
            continue

        if any(token in searchable for token in preferred):
            candidates.append({
                "symbol": pair_symbol,
                "name": name,
            })

    if not candidates:
        raise RuntimeError(
            f"LSE katalogida {symbol} uchun mos ticker topilmadi"
        )

    # Avval XAU/XAG kodi aniq mos kelgan instrumentni tanlaymiz.
    code = "XAU" if symbol == "XAUUSD" else "XAG"

    exact = [
        item for item in candidates
        if code in item["symbol"].upper()
    ]

    selected = exact[0] if exact else candidates[0]

    resolved = {
        "symbol": selected["symbol"],
        "market": settings["market"],
    }

    _resolved_symbols[symbol] = resolved

    logger.info(
        "[LSE SYMBOL] %s -> %s",
        symbol,
        resolved["symbol"],
    )

    return resolved


def _resolve_symbol(client, symbol):
    if symbol in FX_SYMBOLS:
        return FX_SYMBOLS[symbol]

    if symbol in COMMODITY_SEARCH:
        return _resolve_commodity_symbol(client, symbol)

    raise ValueError(
        f"LSE uchun qo'llab-quvvatlanmaydigan instrument: {symbol}"
    )


def _normalize_rows(rows, symbol, timeframe):
    if not isinstance(rows, list):
        raise RuntimeError(
            "LSE candles noto'g'ri format qaytardi"
        )

    duration_ms = TIMEFRAME_DURATION_MS[timeframe]
    now_ms = int(
        datetime.now(timezone.utc).timestamp() * 1000
    )

    by_timestamp = {}

    for item in rows:
        if not isinstance(item, dict):
            continue

        try:
            timestamp = _parse_timestamp(
                item.get("timestamp")
            )

            # Joriy, hali yopilmagan shamni tashlab ketamiz.
            if timestamp + duration_ms > now_ms:
                continue

            open_price = float(item["open"])
            high = float(item["high"])
            low = float(item["low"])
            close = float(item["close"])
            volume = float(item.get("volume", 0) or 0)

            prices = (
                open_price,
                high,
                low,
                close,
            )

            if not all(
                math.isfinite(value) and value > 0
                for value in prices
            ):
                continue

            if high < low:
                continue

            if not low <= open_price <= high:
                continue

            if not low <= close <= high:
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
                source="lse",
            )

            existing = by_timestamp.get(timestamp)

            if existing is not None and existing != candle:
                raise RuntimeError(
                    f"LSE {symbol} {timeframe}: "
                    f"qarama-qarshi duplicate sham"
                )

            by_timestamp[timestamp] = candle

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
        by_timestamp.values(),
        key=lambda candle: candle.timestamp,
    )

    candles = candles[-MAX_CANDLES:]

    if len(candles) < MIN_CANDLES:
        raise RuntimeError(
            f"LSE {symbol} {timeframe}: "
            f"yopilgan shamlar yetarli emas "
            f"({len(candles)}/{MIN_CANDLES})"
        )

    if not all(candle.is_closed for candle in candles):
        raise RuntimeError(
            f"LSE {symbol} {timeframe}: "
            "yopilmagan sham aniqlandi"
        )

    return candles


def get_lse_candles(symbol, timeframe, limit=250):
    if timeframe not in TIMEFRAME_MAP:
        raise ValueError(
            f"LSE timeframe qo'llab-quvvatlanmaydi: {timeframe}"
        )

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not MIN_CANDLES <= limit <= MAX_CANDLES
    ):
        raise ValueError(
            f"LSE limit {MIN_CANDLES}-{MAX_CANDLES} "
            "oralig'ida bo'lishi kerak"
        )

    client = _get_client()
    instrument = _resolve_symbol(client, symbol)

    interval = TIMEFRAME_MAP[timeframe]

    logger.info(
        "[LSE DATA] %s %s so'rovi",
        symbol,
        timeframe,
    )

    try:
        rows = client.candles(
            instrument["symbol"],
            interval,
            limit=limit,
            order="desc",
            market=instrument["market"],
        )

    except Exception as error:
        raise RuntimeError(
            f"LSE {symbol} {timeframe} candles xatosi: {error}"
        ) from error

    candles = _normalize_rows(
        rows=rows,
        symbol=symbol,
        timeframe=timeframe,
    )

    logger.info(
        "[LSE DATA OK] %s %s | candles=%s | source=lse",
        symbol,
        timeframe,
        len(candles),
    )

    return candles
          
