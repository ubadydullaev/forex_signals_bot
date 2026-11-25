
import logging
import warnings
from datetime import datetime, timezone

from . import data_adapter
from .models import MarketData


logger = logging.getLogger("V6.DATA")

MINIMUM_HISTORY = 1000
MAX_FX_DATA_AGE_HOURS = 72
OLD_GAP_AGE_DAYS = 30

# Dukascopy 403 qaytarsa, shu workflow run davomida qayta chaqirilmaydi.
_DUKASCOPY_BLOCKED = False


def _get_providers(symbol):
    if symbol in data_adapter.BINANCE_SYMBOLS:
        return [
            ("Binance Spot", data_adapter._fetch_binance),
        ]

    providers = [
        ("Dukascopy", data_adapter._fetch_dukascopy),
        ("ForexSB", data_adapter._fetch_forexsb),
    ]

    if symbol in data_adapter.TWELVE_DATA_SYMBOLS:
        providers.append(
            ("Twelve Data", data_adapter._fetch_twelve_data_with_cache)
        )

    providers.append(("LSE", data_adapter._fetch_lse))

    return providers


def _format_time(timestamp):
    return datetime.fromtimestamp(
        timestamp / 1000,
        tz=timezone.utc,
    ).isoformat()


def _gap_age_days(gap, now_ms):
    gap_end = int(gap.get("to", 0))
    if gap_end <= 0:
        return -1
    return (now_ms - gap_end) / 86_400_000


def _is_tolerable_old_gap(gap, timeframe, source, now_ms):
    if source != "forexsb":
        return False

    if gap.get("missing_intervals") != 1:
        return False

    if _gap_age_days(gap, now_ms) <= OLD_GAP_AGE_DAYS:
        return False

    return timeframe in ("M15", "M30", "H1", "H4", "D1")


def _validate_history(symbol, timeframe, candles, requested_limit):
    if not isinstance(candles, list):
        raise RuntimeError("Manba candle ro'yxatini qaytarmadi")

    if len(candles) < requested_limit:
        raise RuntimeError(
            f"Tarix yetarli emas: {len(candles)}/{requested_limit} sham"
        )

    if not all(candle.is_closed for candle in candles):
        raise RuntimeError("Yopilmagan sham aniqlandi")

    sources = {candle.source for candle in candles}
    if len(sources) != 1:
        raise RuntimeError(
            f"Bir timeframe ichida turli manbalar aralashgan: {sources}"
        )

    for previous, current in zip(candles, candles[1:]):
        if current.timestamp <= previous.timestamp:
            raise RuntimeError("Shamlar vaqti tartibsiz yoki takrorlangan")

    source = candles[-1].source
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        gaps = data_adapter._inspect_candle_gaps(
            candles, timeframe, source, symbol
        )

    accepted_gaps = []
    rejected_gaps = []

    for gap in gaps:
        if _is_tolerable_old_gap(gap, timeframe, source, now_ms):
            accepted_gaps.append(gap)
        else:
            rejected_gaps.append(gap)

    for gap in accepted_gaps:
        logger.warning(
            "[OLD ISOLATED GAP ACCEPTED] %s %s | age_days=%.1f | gap=%s",
            symbol,
            timeframe,
            _gap_age_days(gap, now_ms),
            gap,
        )

    if rejected_gaps:
        raise RuntimeError(
            f"Kutilmagan sham uzilishi: {len(rejected_gaps)} ta. "
            f"Birinchi uzilishlar: {rejected_gaps[:3]}"
        )

    duration_ms = data_adapter.TIMEFRAME_SETTINGS[timeframe]["duration_ms"]
    last_candle = candles[-1]
    last_close_time = last_candle.timestamp + duration_ms

    age_ms = max(0, now_ms - last_close_time)
    age_hours = age_ms / 3_600_000

    if symbol in data_adapter.BINANCE_SYMBOLS:
        max_age_ms = max(2 * duration_ms, 30 * 60 * 1000)
    else:
        max_age_ms = MAX_FX_DATA_AGE_HOURS * 3_600_000

    if age_ms > max_age_ms:
        raise RuntimeError(
            f"Eskirgan ma'lumot: oxirgi sham yoshi {age_hours:.2f} soat"
        )

    logger.info(
        "[DATA QUALITY PASS] %s %s | candles=%s | source=%s | "
        "first=%s | last=%s | age_hours=%.2f | accepted_old_gaps=%s",
        symbol,
        timeframe,
        len(candles),
        source,
        _format_time(candles[0].timestamp),
        _format_time(last_candle.timestamp),
        age_hours,
        len(accepted_gaps),
    )

    if symbol in data_adapter.BINANCE_SYMBOLS:
        logger.warning(
            "[CRYPTO FEED NOTE] %s Binance Spot %s. "
            "Broker CFD kotirovkasi bilan farq qilishi mumkin.",
            symbol,
            data_adapter.BINANCE_SYMBOLS[symbol],
        )


def _is_dukascopy_403(error):
    message = str(error).lower()
    return (
        "http 403" in message
        or "refused access (http 403)" in message
    )


def get_verified_market_data(
    symbol,
    timeframe,
    limit=MINIMUM_HISTORY,
):
    global _DUKASCOPY_BLOCKED

    if limit < MINIMUM_HISTORY:
        raise ValueError(
            f"V6 uchun kamida {MINIMUM_HISTORY} ta sham talab qilinadi"
        )

    data_adapter._validate_request(symbol, timeframe, limit)

    errors = {}

    for provider_name, provider_function in _get_providers(symbol):

        if provider_name == "Dukascopy" and _DUKASCOPY_BLOCKED:
            logger.warning(
                "[PROVIDER SKIPPED] %s %s | Dukascopy oldingi HTTP 403 "
                "sababli shu run davomida vaqtincha o'tkazib yuborildi",
                symbol,
                timeframe,
            )
            continue

        try:
            logger.info(
                "[DATA SOURCE TRY] %s %s | provider=%s | requested=%s",
                symbol,
                timeframe,
                provider_name,
                limit,
            )

            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always")
                candles = provider_function(symbol, timeframe, limit)

            for warning in captured:
                logger.warning(
                    "[PROVIDER WARNING] %s %s | %s",
                    symbol,
                    timeframe,
                    warning.message,
                )

            _validate_history(symbol, timeframe, candles, limit)

            logger.info(
                "[DATA SOURCE SELECTED] %s %s | %s",
                symbol,
                timeframe,
                provider_name,
            )

            return MarketData(
                symbol=symbol,
                timeframe=timeframe,
                candles=candles,
            )

        except Exception as error:
            errors[provider_name] = str(error)

            if (
                provider_name == "Dukascopy"
                and _is_dukascopy_403(error)
            ):
                _DUKASCOPY_BLOCKED = True
                logger.error(
                    "[DUKASCOPY CIRCUIT BREAKER] HTTP 403 aniqlandi. "
                    "Qolgan instrumentlarda Dukascopy o'tkazib yuboriladi."
                )

            logger.warning(
                "[DATA SOURCE REJECTED] %s %s | %s | %s",
                symbol,
                timeframe,
                provider_name,
                error,
            )

    details = " | ".join(
        f"{name}: {message}" for name, message in errors.items()
    )

    raise RuntimeError(
        f"{symbol} {timeframe}: {limit} ta sifatli sham topilmadi. "
        f"{details}"
    )
