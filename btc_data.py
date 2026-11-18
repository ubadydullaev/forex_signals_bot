
import math
import requests

from datetime import datetime, timezone


BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"

BTC_INTERVALS = {
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}

BTC_CANDLE_LIMIT = 1000
BTC_MIN_CANDLES = 220


def get_btc_candles(interval, limit=BTC_CANDLE_LIMIT):

    if interval not in BTC_INTERVALS:
        raise ValueError(
            f"BTC uchun noto'g'ri timeframe: {interval}"
        )

    if not 220 <= limit <= 1000:
        raise ValueError(
            "Shamlar soni 220 dan 1000 gacha bo'lishi kerak"
        )

    candle_ms = BTC_INTERVALS[interval]

    params = {
        "symbol": "BTCUSDT",
        "interval": interval,
        "limit": limit,
    }

    response = requests.get(
        BINANCE_URL,
        params=params,
        timeout=20
    )

    response.raise_for_status()

    raw_data = response.json()

    if not isinstance(raw_data, list):
        raise RuntimeError(
            "Binance noto'g'ri ma'lumot qaytardi"
        )

    now_ms = int(
        datetime.now(timezone.utc).timestamp() * 1000
    )

    candles_by_time = {}

    for item in raw_data:

        try:
            timestamp = int(item[0])
            close_time = int(item[6])

            # Faqat to'liq yopilgan shamlar
            if close_time >= now_ms:
                continue

            candle = {
                "time": datetime.fromtimestamp(
                    timestamp / 1000,
                    timezone.utc
                ).isoformat(),

                "timestamp": timestamp,

                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
            }

            prices = (
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
            )

            if not all(
                math.isfinite(x) and x > 0
                for x in prices
            ):
                continue

            if candle["high"] < candle["low"]:
                continue

            if not (
                candle["low"]
                <= candle["open"]
                <= candle["high"]
            ):
                continue

            if not (
                candle["low"]
                <= candle["close"]
                <= candle["high"]
            ):
                continue

            candles_by_time[timestamp] = candle

        except (
            IndexError,
            TypeError,
            ValueError,
            OverflowError
        ):
            continue

    candles = sorted(
        candles_by_time.values(),
        key=lambda x: x["timestamp"]
    )

    if len(candles) < BTC_MIN_CANDLES:
        raise RuntimeError(
            f"BTC tarixiy shamlar yetarli emas: "
            f"{len(candles)}/{BTC_MIN_CANDLES}"
        )

    return candles[-limit:]
              
