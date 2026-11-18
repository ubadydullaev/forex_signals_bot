import os
import json
import time
import base64
import logging
import subprocess

from datetime import datetime, timedelta, timezone

import requests
import pandas as pd
from advanced_analysis import analyze_advanced
from btc_data import get_btc_candles
from signal_tracker import (
    register_tracked_signal,
    evaluate_tracked_signals,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = str(os.environ["TELEGRAM_CHAT_ID"])

WORKER_URL = os.environ["CLOUDFLARE_WORKER_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPOSITORY = os.environ["GITHUB_REPOSITORY"]
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

STATE_FILE = "bot_v4_state.json"

SYMBOLS = [
    "XAUUSD",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "USDCHF",
    "US500",
    "BTCUSD",
    "DE30",
    "XAGUSD",
    "BRENT",
]

DUKASCOPY_SYMBOLS = {
    "XAUUSD": "xauusd",
    "EURUSD": "eurusd",
    "GBPUSD": "gbpusd",
    "USDJPY": "usdjpy",
    "AUDUSD": "audusd",
    "USDCAD": "usdcad",
    "USDCHF": "usdchf",
    "US500": "usa500idxusd",
    "DE30": "deuidxeur",
    "XAGUSD": "xagusd",
    "BRENT": "brentcmdusd",
}

TIMEFRAMES = {
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
}

ATR_PERIOD = 14

MIN_SCORE = 78

MIN_REWARD_RISK = 1.5
PREFERRED_REWARD_RISK = 1.5

SL_ATR_MIN = 1.2
SL_ATR_MAX = 2.2

REFERENCE_LOT = 0.01
CANDLE_LIMIT = 1000

MAX_TP_LEVELS = 20

SUPPORT_CLUSTER_ATR = 0.20
FVG_MIN_ATR = 0.10
FVG_IMPULSE_ATR = 0.50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()

session.headers.update({
    "User-Agent": "ForexSignalsBot/4.4"
})


# =========================================================
# TIME HELPERS
# =========================================================

def time_key(value):

    value = str(value)

    try:
        return (0, float(value))

    except ValueError:

        try:
            dt = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )

            return (1, dt.timestamp())

        except ValueError:
            return (2, value)


# =========================================================
# MARKET DATA
# =========================================================

def get_candles(symbol, interval):
    if symbol == "BTCUSD":
        return get_btc_candles(interval)
    timeframe_map = {
    "15m": ("m15", 7, 15 * 60 * 1000),
    "30m": ("m30", 10, 30 * 60 * 1000),
    "1h": ("h1", 20, 60 * 60 * 1000),
    "4h": ("h4", 75, 4 * 60 * 60 * 1000),
    "1d": ("d1", 500, 24 * 60 * 60 * 1000),
}

    if interval not in timeframe_map:
        raise ValueError(
            f"Noto'g'ri timeframe: {interval}"
        )

    dukascopy_tf, lookback_days, candle_ms = (
        timeframe_map[interval]
    )

    dukascopy_symbol = DUKASCOPY_SYMBOLS.get(symbol)

    if not dukascopy_symbol:
        raise ValueError(
            f"Dukascopy kodi topilmadi: {symbol}"
        )

    now = datetime.now(timezone.utc)

    from_date = (
        now - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")

    to_date = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    output_dir = os.path.abspath("download")

    os.makedirs(output_dir, exist_ok=True)

    file_name = (
        f"{dukascopy_symbol}_{dukascopy_tf}_"
        f"{now.strftime('%Y%m%d%H%M')}"
    )

    output_file = os.path.join(
        output_dir,
        file_name + ".json"
    )

    command = [
        "npx",
        "--no-install",
        "dukascopy-node",
        "-i",
        dukascopy_symbol,
        "-from",
        from_date,
        "-to",
        to_date,
        "-t",
        dukascopy_tf,
        "-f",
        "json",
        "-dir",
        output_dir,
        "-fn",
        file_name,
        "--cache",
        "--retries",
        "3",
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=240
    )

    if result.returncode != 0:

        diagnostic = "\n".join(
            x for x in (
                result.stdout,
                result.stderr
            )
            if x and x.strip()
        )

        raise RuntimeError(
            "Dukascopy xatosi: "
            + (
                diagnostic[-2500:]
                or "CLI hech qanday xabar bermadi"
            )
        )

    if not os.path.exists(output_file):
        raise RuntimeError(
            "Dukascopy JSON fayl yaratilmadi"
        )

    with open(
        output_file,
        "r",
        encoding="utf-8"
    ) as f:

        raw_data = json.load(f)

    if not isinstance(raw_data, list):
        raise RuntimeError(
            "Dukascopy noto'g'ri JSON format qaytardi"
        )

    now_ms = int(now.timestamp() * 1000)

    candles_by_time = {}

    for item in raw_data:

        try:

            timestamp = int(item["timestamp"])

            # Faqat to'liq yopilgan shamlar
            if timestamp + candle_ms > now_ms:
                continue

            candle = {
                "time": datetime.fromtimestamp(
                    timestamp / 1000,
                    timezone.utc
                ).isoformat(),

                "timestamp": timestamp,

                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            }

            prices = (
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"]
            )

            if not all(
                x > 0 and x < float("inf")
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
            KeyError,
            TypeError,
            ValueError,
            OverflowError
        ):
            continue

    candles = sorted(
        candles_by_time.values(),
        key=lambda x: x["timestamp"]
    )

    if len(candles) < 220:
        raise RuntimeError(
            f"Yetarli yopilgan sham yo'q: "
            f"{len(candles)}/220"
        )

    return candles[-CANDLE_LIMIT:]
# =========================================================
# ADVANCED ANALYSIS DATA
# =========================================================

def run_advanced_analysis(timeframe, candles, h1, h4, d1):

    entry_df = pd.DataFrame(candles)
    higher_timeframes = {}

    if timeframe in ("M15", "M30", "H1"):
        if h1:
            higher_timeframes["H1"] = pd.DataFrame(h1)

    if timeframe in ("M15", "M30", "H1"):
        if h4:
            higher_timeframes["H4"] = pd.DataFrame(h4)

    if timeframe in ("M15", "M30", "H1", "H4"):
        if d1:
            higher_timeframes["D1"] = pd.DataFrame(d1)

    return analyze_advanced(
        entry_df=entry_df,
        higher_timeframes=higher_timeframes,
    )

# =========================================================
# INDICATORS
# =========================================================

def ema_series(values, period):

    out = [None] * len(values)

    if len(values) < period:
        return out

    current = sum(values[:period]) / period

    out[period - 1] = current

    alpha = 2 / (period + 1)

    for i in range(period, len(values)):

        current = (
            values[i] * alpha
            + current * (1 - alpha)
        )

        out[i] = current

    return out


def rsi_series(values, period=14):

    out = [None] * len(values)

    if len(values) <= period:
        return out

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [
        max(x, 0)
        for x in changes
    ]

    losses = [
        max(-x, 0)
        for x in changes
    ]

    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    def calculate(g, l):

        if l == 0 and g == 0:
            return 50.0

        if l == 0:
            return 100.0

        if g == 0:
            return 0.0

        rs = g / l

        return 100 - 100 / (1 + rs)

    out[period] = calculate(
        avg_g,
        avg_l
    )

    for i in range(period + 1, len(values)):

        avg_g = (
            avg_g * (period - 1)
            + gains[i - 1]
        ) / period

        avg_l = (
            avg_l * (period - 1)
            + losses[i - 1]
        ) / period

        out[i] = calculate(
            avg_g,
            avg_l
        )

    return out


def true_ranges(candles):

    result = []

    for i, candle in enumerate(candles):

        if i == 0:

            result.append(
                candle["high"] - candle["low"]
            )

        else:

            previous_close = (
                candles[i - 1]["close"]
            )

            result.append(
                max(
                    candle["high"] - candle["low"],
                    abs(
                        candle["high"]
                        - previous_close
                    ),
                    abs(
                        candle["low"]
                        - previous_close
                    )
                )
            )

    return result


def atr_series(candles, period=14):

    tr = true_ranges(candles)

    out = [None] * len(candles)

    if len(tr) < period:
        return out

    value = sum(tr[:period]) / period

    out[period - 1] = value

    for i in range(period, len(tr)):

        value = (
            value * (period - 1)
            + tr[i]
        ) / period

        out[i] = value

    return out


# =========================================================
# ADX + DIRECTIONAL INDICATORS
# =========================================================

def adx_data(candles, period=14):

    if len(candles) < period * 2 + 1:
        return None, None, None

    tr = true_ranges(candles)

    plus_dm = [0.0] * len(candles)
    minus_dm = [0.0] * len(candles)

    for i in range(1, len(candles)):

        up = (
            candles[i]["high"]
            - candles[i - 1]["high"]
        )

        down = (
            candles[i - 1]["low"]
            - candles[i]["low"]
        )

        if up > down and up > 0:
            plus_dm[i] = up

        elif down > up and down > 0:
            minus_dm[i] = down

    smoothed_tr = sum(
        tr[1:period + 1]
    )

    smoothed_plus = sum(
        plus_dm[1:period + 1]
    )

    smoothed_minus = sum(
        minus_dm[1:period + 1]
    )

    dx_values = []

    last_plus_di = None
    last_minus_di = None

    for i in range(period, len(candles)):

        if i > period:

            smoothed_tr = (
                smoothed_tr
                - smoothed_tr / period
                + tr[i]
            )

            smoothed_plus = (
                smoothed_plus
                - smoothed_plus / period
                + plus_dm[i]
            )

            smoothed_minus = (
                smoothed_minus
                - smoothed_minus / period
                + minus_dm[i]
            )

        if smoothed_tr <= 0:

            dx_values.append(0.0)

            last_plus_di = 0.0
            last_minus_di = 0.0

            continue

        plus_di = (
            100 * smoothed_plus / smoothed_tr
        )

        minus_di = (
            100 * smoothed_minus / smoothed_tr
        )

        last_plus_di = plus_di
        last_minus_di = minus_di

        denominator = plus_di + minus_di

        if denominator > 0:

            dx = (
                100
                * abs(plus_di - minus_di)
                / denominator
            )

        else:
            dx = 0.0

        dx_values.append(dx)

    if len(dx_values) < period:
        return None, None, None

    adx = sum(
        dx_values[:period]
    ) / period

    for dx in dx_values[period:]:

        adx = (
            adx * (period - 1) + dx
        ) / period

    return (
        adx,
        last_plus_di,
        last_minus_di
    )


def adx_value(candles, period=14):

    adx, _, _ = adx_data(
        candles,
        period
    )

    return adx


# =========================================================
# SUPERTREND
# =========================================================

def supertrend_direction(
    candles,
    atrs,
    factor=2.5
):

    final_upper = None
    final_lower = None

    direction = 0

    previous_valid_index = None

    for i, candle in enumerate(candles):

        atr = atrs[i]

        if atr is None or atr <= 0:
            continue

        middle = (
            candle["high"] + candle["low"]
        ) / 2

        basic_upper = middle + factor * atr
        basic_lower = middle - factor * atr

        if final_upper is None:

            final_upper = basic_upper
            final_lower = basic_lower

            direction = (
                1
                if candle["close"] >= middle
                else -1
            )

            previous_valid_index = i

            continue

        previous_close = candles[
            previous_valid_index
        ]["close"]

        previous_upper = final_upper
        previous_lower = final_lower

        if (
            basic_upper < previous_upper
            or previous_close > previous_upper
        ):

            final_upper = basic_upper

        else:
            final_upper = previous_upper

        if (
            basic_lower > previous_lower
            or previous_close < previous_lower
        ):

            final_lower = basic_lower

        else:
            final_lower = previous_lower

        if (
            direction == -1
            and candle["close"] > previous_upper
        ):

            direction = 1

        elif (
            direction == 1
            and candle["close"] < previous_lower
        ):

            direction = -1

        previous_valid_index = i

    return direction


# =========================================================
# TREND DIRECTION
# =========================================================

def trend_direction(candles):

    if len(candles) < 200:
        return 0

    closes = [
        x["close"]
        for x in candles
    ]

    e20 = ema_series(closes, 20)[-1]
    e50 = ema_series(closes, 50)[-1]
    e200 = ema_series(closes, 200)[-1]

    if None in (e20, e50, e200):
        return 0

    if (
        e20 > e50 > e200
        and closes[-1] > e20
    ):
        return 1

    if (
        e20 < e50 < e200
        and closes[-1] < e20
    ):
        return -1

    return 0


# =========================================================
# SUPPORT / RESISTANCE CLUSTERING
# =========================================================

def cluster_levels(levels, tolerance):

    if not levels:
        return []

    levels = sorted(levels)

    clusters = []

    current_cluster = [levels[0]]

    for level in levels[1:]:

        average = sum(
            current_cluster
        ) / len(current_cluster)

        if abs(level - average) <= tolerance:

            current_cluster.append(level)

        else:

            clusters.append(
                sum(current_cluster)
                / len(current_cluster)
            )

            current_cluster = [level]

    clusters.append(
        sum(current_cluster)
        / len(current_cluster)
    )

    return clusters


# =========================================================
# MARKET STRUCTURE
# =========================================================

def structure_data(candles, atr=None):

    empty = {
        "buy": 0,
        "sell": 0,
        "swing_low": None,
        "swing_high": None,
        "support_levels": [],
        "resistance_levels": [],
    }

    if len(candles) < 30:
        return empty

    data = candles[:-1]
    current = candles[-1]

    pivot_highs = []
    pivot_lows = []

    # Confirmed pivots: 2 candles left and right
    for i in range(2, len(data) - 2):

        high = data[i]["high"]
        low = data[i]["low"]

        if (
            high > data[i - 1]["high"]
            and high > data[i - 2]["high"]
            and high >= data[i + 1]["high"]
            and high >= data[i + 2]["high"]
        ):

            pivot_highs.append({
                "index": i,
                "price": high,
            })

        if (
            low < data[i - 1]["low"]
            and low < data[i - 2]["low"]
            and low <= data[i + 1]["low"]
            and low <= data[i + 2]["low"]
        ):

            pivot_lows.append({
                "index": i,
                "price": low,
            })

    entry = current["close"]

    cutoff = max(
        0,
        len(data) - 200
    )

    recent_highs = [
        p for p in pivot_highs
        if p["index"] >= cutoff
    ]

    recent_lows = [
        p for p in pivot_lows
        if p["index"] >= cutoff
    ]

    tolerance = (
        max(atr * SUPPORT_CLUSTER_ATR, 0)
        if atr is not None
        else 0
    )

    resistance_levels = cluster_levels(
        [
            p["price"]
            for p in recent_highs
            if p["price"] > entry
        ],
        tolerance
    )

    support_levels = cluster_levels(
        [
            p["price"]
            for p in recent_lows
            if p["price"] < entry
        ],
        tolerance
    )

    resistance_levels = sorted(
        resistance_levels
    )

    support_levels = sorted(
        support_levels,
        reverse=True
    )

    buy_swing_candidates = [
        p for p in recent_lows
        if p["price"] < entry
    ]

    sell_swing_candidates = [
        p for p in recent_highs
        if p["price"] > entry
    ]

    swing_low = (
        max(
            buy_swing_candidates,
            key=lambda p: p["price"]
        )["price"]
        if buy_swing_candidates
        else None
    )

    swing_high = (
        min(
            sell_swing_candidates,
            key=lambda p: p["price"]
        )["price"]
        if sell_swing_candidates
        else None
    )

    buy = 0
    sell = 0

    # -----------------------------------------------------
    # 1. Liquidity sweep using confirmed pivots
    # -----------------------------------------------------

    recent_pivot_highs = [
        p for p in recent_highs
        if p["price"] > current["close"]
    ][-5:]

    recent_pivot_lows = [
        p for p in recent_lows
        if p["price"] < current["close"]
    ][-5:]

    bullish_sweep = any(
        current["low"] < p["price"]
        and current["close"] > p["price"]
        for p in recent_pivot_lows
    )

    bearish_sweep = any(
        current["high"] > p["price"]
        and current["close"] < p["price"]
        for p in recent_pivot_highs
    )

    if bullish_sweep:
        buy += 1

    if bearish_sweep:
        sell += 1

    # -----------------------------------------------------
    # 2. Three-candle FVG + impulse filter
    # -----------------------------------------------------

    first = candles[-3]
    middle = candles[-2]

    middle_body = abs(
        middle["close"] - middle["open"]
    )

    impulse_ok = (
        atr is not None
        and atr > 0
        and middle_body >= atr * FVG_IMPULSE_ATR
    )

    bullish_gap = (
        current["low"] - first["high"]
    )

    bearish_gap = (
        first["low"] - current["high"]
    )

    bullish_fvg = (
        impulse_ok
        and bullish_gap >= atr * FVG_MIN_ATR
        and middle["close"] > middle["open"]
    )

    bearish_fvg = (
        impulse_ok
        and bearish_gap >= atr * FVG_MIN_ATR
        and middle["close"] < middle["open"]
    )

    if bullish_fvg:
        buy += 1

    if bearish_fvg:
        sell += 1

    # -----------------------------------------------------
    # 3. Strong candle close
    # -----------------------------------------------------

    candle_range = (
        current["high"] - current["low"]
    )

    if candle_range > 0:

        close_position = (
            current["close"] - current["low"]
        ) / candle_range

        if (
            current["close"] > current["open"]
            and close_position >= 0.75
        ):

            buy += 1

        elif (
            current["close"] < current["open"]
            and close_position <= 0.25
        ):

            sell += 1

    return {
        "buy": buy,
        "sell": sell,
        "swing_low": swing_low,
        "swing_high": swing_high,
        "support_levels": support_levels,
        "resistance_levels": resistance_levels,
    }


# =========================================================
# LEVEL FILTER
# =========================================================

def filter_levels(
    levels,
    entry,
    risk,
    atr,
    side
):

    valid = []

    for level in levels:

        if side == "BUY":
            distance = level - entry

        else:
            distance = entry - level

        if distance <= 0:
            continue

        rr = distance / risk

        if rr < MIN_REWARD_RISK:
            continue

        valid.append(level)

    if side == "BUY":
        valid.sort()

    else:
        valid.sort(reverse=True)

    filtered = []

    minimum_gap = atr * 0.25

    for level in valid:

        if not filtered:

            filtered.append(level)
            continue

        if (
            abs(level - filtered[-1])
            >= minimum_gap
        ):

            filtered.append(level)

    return filtered


# =========================================================
# PRICE FORMATTING
# =========================================================

def price_digits(symbol):

    if symbol in ("US500", "DE30"):
        return 1
    if symbol == "BTCUSD":
        return 2
    if symbol == "XAUUSD":
        return 2

    if symbol == "XAGUSD":
        return 3

    if symbol == "BRENT":
        return 2

    if symbol.endswith("JPY"):
        return 3

    return 5


# =========================================================
# USD RISK ESTIMATE
# =========================================================

def usd_value_per_price_unit(symbol, entry):

    """
    0.01 lot uchun taxminiy USD qiymati.
    Broker kontrakt hajmi farq qilishi mumkin.
    """

    if symbol == "XAUUSD":
        return 1.0

    if symbol in (
        "EURUSD",
        "GBPUSD",
        "AUDUSD",
    ):
        return 1000.0

    if symbol in (
        "USDJPY",
        "USDCHF",
        "USDCAD",
    ):

        if entry <= 0:
            return None

        return 1000.0 / entry

    return None



# =========================================================
# SIGNAL ANALYSIS
# =========================================================

def analyze(symbol, timeframe, candles, h1, h4):

    if (
        len(candles) < 220
        or len(h1) < 220
        or len(h4) < 220
    ):
        return None

    closes = [
        x["close"]
        for x in candles
    ]

    current = candles[-1]
    entry = current["close"]

    e20 = ema_series(closes, 20)[-1]
    e50 = ema_series(closes, 50)[-1]
    e200 = ema_series(closes, 200)[-1]

    rsi = rsi_series(closes)[-1]

    atrs = atr_series(
        candles,
        ATR_PERIOD
    )

    atr = atrs[-1]

    adx, plus_di, minus_di = adx_data(
        candles
    )

    if any(
        x is None
        for x in (
            e20,
            e50,
            e200,
            rsi,
            atr,
            adx,
            plus_di,
            minus_di,
        )
    ):
        return None

    if atr <= 0 or adx < 18:
        return None

    st = supertrend_direction(
        candles,
        atrs
    )

    h1_dir = trend_direction(h1)
    h4_dir = trend_direction(h4)

    structure = structure_data(
        candles,
        atr
    )

    buy = 0
    sell = 0

    # 1. EMA 20 / EMA 50
    if e20 > e50:
        buy += 15

    elif e20 < e50:
        sell += 15

    # 2. EMA 200
    if entry > e200:
        buy += 10

    elif entry < e200:
        sell += 10

    # 3. Supertrend
    if st == 1:
        buy += 10

    elif st == -1:
        sell += 10

    # 4. RSI
    if 52 <= rsi <= 68:
        buy += 10

    elif 32 <= rsi <= 48:
        sell += 10

    # 5. ADX strength + DI direction
    if adx >= 22:

        if plus_di > minus_di:
            buy += 10

        elif minus_di > plus_di:
            sell += 10

    elif adx >= 18:

        if plus_di > minus_di:
            buy += 5

        elif minus_di > plus_di:
            sell += 5

    # 6. H1
    if h1_dir == 1:
        buy += 15

    elif h1_dir == -1:
        sell += 15

    # 7. H4
    if h4_dir == 1:
        buy += 15

    elif h4_dir == -1:
        sell += 15

    # 8. Structure
    buy += min(
        15,
        structure["buy"] * 5
    )

    sell += min(
        15,
        structure["sell"] * 5
    )

    # -----------------------------------------------------
    # Signal direction
    # -----------------------------------------------------

    if (
        buy >= MIN_SCORE
        and buy > sell
        and h1_dir == 1
        and h4_dir == 1
    ):

        side = "BUY"
        score = buy

    elif (
        sell >= MIN_SCORE
        and sell > buy
        and h1_dir == -1
        and h4_dir == -1
    ):

        side = "SELL"
        score = sell

    else:
        return None

    # =====================================================
    # STOP LOSS
    # =====================================================

    if side == "BUY":

        structural_sl = structure["swing_low"]

        atr_sl = entry - atr * 1.5

        if structural_sl is not None:

            candidate_sl = (
                structural_sl - atr * 0.15
            )

        else:
            candidate_sl = atr_sl

        candidate_risk = entry - candidate_sl

        if (
            candidate_risk <= 0
            or candidate_risk < atr * SL_ATR_MIN
            or candidate_risk > atr * SL_ATR_MAX
        ):

            sl = atr_sl

        else:
            sl = candidate_sl

        risk = entry - sl

    else:

        structural_sl = structure["swing_high"]

        atr_sl = entry + atr * 1.5

        if structural_sl is not None:

            candidate_sl = (
                structural_sl + atr * 0.15
            )

        else:
            candidate_sl = atr_sl

        candidate_risk = candidate_sl - entry

        if (
            candidate_risk <= 0
            or candidate_risk < atr * SL_ATR_MIN
            or candidate_risk > atr * SL_ATR_MAX
        ):

            sl = atr_sl

        else:
            sl = candidate_sl

        risk = sl - entry

    if (
        risk <= 0
        or risk < atr * SL_ATR_MIN
        or risk > atr * SL_ATR_MAX
    ):
        return None

    # =====================================================
    # STRUCTURAL TAKE PROFITS
    # =====================================================

    if side == "BUY":
        levels = structure["resistance_levels"]

    else:
        levels = structure["support_levels"]

    valid_levels = filter_levels(
        levels,
        entry,
        risk,
        atr,
        side
    )

    if not valid_levels:
        return None

    preferred_levels = [
        level
        for level in valid_levels
        if abs(level - entry) / risk
        >= PREFERRED_REWARD_RISK
    ]

    if preferred_levels:
        first_tp = preferred_levels[0]

    else:
        first_tp = valid_levels[0]

    if side == "BUY":

        tps = [
            level
            for level in valid_levels
            if level >= first_tp
        ]

    else:

        tps = [
            level
            for level in valid_levels
            if level <= first_tp
        ]

    final_tps = []

    minimum_gap = atr * 0.25

    for level in tps:

        if not final_tps:

            final_tps.append(level)
            continue

        if (
            abs(level - final_tps[-1])
            >= minimum_gap
        ):

            final_tps.append(level)

    final_tps = final_tps[:MAX_TP_LEVELS]

    if not final_tps:
        return None

    first_rr = (
        abs(final_tps[0] - entry) / risk
    )

    if first_rr < MIN_REWARD_RISK:
        return None

    # =====================================================
    # USD RISK / REWARD
    # =====================================================

    usd_factor = usd_value_per_price_unit(
        symbol,
        entry
    )

    # Indeks va tovarlar uchun broker kontrakt qiymati
    # tasdiqlanmaguncha dollar hisobini ko'rsatmaymiz.
    risk_usd = (
        risk * usd_factor
        if usd_factor is not None
        else None
    )

    rewards_usd = [
        (
            abs(tp - entry) * usd_factor
            if usd_factor is not None
            else None
        )
        for tp in final_tps
    ]

    reward_risks = [
        abs(tp - entry) / risk
        for tp in final_tps
    ]

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "score": score,

        "entry": entry,
        "sl": sl,
        "risk": risk,

        "tp1": final_tps[0],
        "tps": final_tps,

        "risk_usd": risk_usd,
        "rewards_usd": rewards_usd,
        "reward_risks": reward_risks,

        "candle_time": current["time"],

        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "rsi": rsi,
        "atr": atr,
        "atr_risk_ratio": risk / atr,
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "supertrend": st,
        "h1_direction": h1_dir,
        "h4_direction": h4_dir,
        "structure_buy": structure["buy"],
        "structure_sell": structure["sell"],
    }
# =========================================================
# TELEGRAM
# =========================================================

def get_users():

    response = session.get(
        f"{WORKER_URL}/approved-users",

        headers={
            "X-Worker-Secret": WEBHOOK_SECRET
        },

        timeout=20
    )

    response.raise_for_status()

    users = response.json().get(
        "users",
        []
    )

    return sorted(
        set(
            [ADMIN_ID]
            + [str(x) for x in users]
        )
    )


def send_telegram(chat_id, message):

    response = session.post(
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage",

        json={
            "chat_id": str(chat_id),
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },

        timeout=20
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(str(result))

    return True


def format_signal(signal):

    symbol = signal["symbol"]

    digits = price_digits(symbol)

    emoji = (
        "🟢"
        if signal["side"] == "BUY"
        else "🔴"
    )

    def fmt(value):
        return f"{value:.{digits}f}"

    lines = [
        f"{emoji} <b>{symbol} — {signal['side']}</b>",
        f"⏱ Timeframe: <b>{signal['timeframe']}</b>",
        "━━━━━━━━━━━━━━",
        f"🎯 Entry: <code>{fmt(signal['entry'])}</code>",
        f"🛑 SL: <code>{fmt(signal['sl'])}</code>",
        "",
        "<b>TAKE PROFITS</b>",
    ]

    for i, tp in enumerate(signal["tps"]):

        reward = signal["rewards_usd"][i]
        rr = signal["reward_risks"][i]

        reward_text = (
            f"~${reward:.2f} | "
            if reward is not None
            else ""
        )

        lines.append(
            f"✅ TP{i + 1}: "
            f"<code>{fmt(tp)}</code> "
            f"({reward_text}{rr:.2f}R)"
        )

    risk_line = (
        "⚠️ 0.01 lot uchun taxminiy "
        f"SL riski: <b>${signal['risk_usd']:.2f}</b>"
        if signal["risk_usd"] is not None
        else (
            "📏 SL masofasi: "
            f"<b>{signal['risk']:.{digits}f}</b> "
            "narx birligi"
        )
    )

    lines.extend([
        "━━━━━━━━━━━━━━",

        risk_line,

        (
            "📊 Confluence score: "
            f"<b>{signal['score']}/100</b>"
        ),

        (
            f"ADX: {signal['adx']:.1f} "
            f"| +DI: {signal['plus_di']:.1f} "
            f"| -DI: {signal['minus_di']:.1f}"
        ),

        f"RSI: {signal['rsi']:.1f}",

        "EMA + RSI + ADX + Supertrend",

        "HTF trend + structure + liquidity + FVG",

        "🕯 Yopilgan sham asosida",

        (
            "⚠️ Dollar riski faqat qo'llab-quvvatlangan "
            "instrumentlar uchun taxminiy hisoblanadi. "
            "Indeks va tovarlarda R nisbati ko'rsatiladi."
        ),

        (
            "⚠️ Avval demo hisobda tekshiring. "
            "Bu avtomatik savdo emas."
        ),
    ])

    return "\n".join(lines)


# =========================================================
# GITHUB STATE
# =========================================================

def state_url():

    return (
        f"https://api.github.com/repos/{REPOSITORY}"
        f"/contents/{STATE_FILE}"
    )


def github_headers():

    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_state():

    response = session.get(
        state_url(),

        headers=github_headers(),

        params={
            "ref": BRANCH
        },

        timeout=20
    )

    if response.status_code == 404:

        return {
            "signals": {}
        }, None

    response.raise_for_status()

    data = response.json()

    raw = base64.b64decode(
        data["content"]
    ).decode("utf-8")

    state = json.loads(raw)

    if not isinstance(state, dict):
        raise RuntimeError("State formati noto'g'ri")

    state.setdefault("signals", {})

    if not isinstance(state["signals"], dict):
        raise RuntimeError("State signals formati noto'g'ri")

    return state, data["sha"]


def save_state(state, sha):

    raw = json.dumps(
        state,
        ensure_ascii=False,
        indent=2
    ).encode("utf-8")

    payload = {
        "message": "Update Forex V4.4 signal state",

        "content": base64.b64encode(
            raw
        ).decode("utf-8"),

        "branch": BRANCH,
    }

    if sha:
        payload["sha"] = sha

    response = session.put(
        state_url(),

        headers=github_headers(),

        json=payload,

        timeout=25
    )

    response.raise_for_status()

    result = response.json()

    content = result.get("content") or {}

    return content.get("sha", sha)


# =========================================================
# SIGNAL DELIVERY STATE
# =========================================================

def get_delivery_record(state, key, signature, users):

    old = state["signals"].get(key)

    # Eski format: key -> signature
    # Eski signal allaqachon yuborilgan deb olinadi.
    if isinstance(old, str):

        if old == signature:
            return None

        return {
            "signature": signature,
            "target_users": list(users),
            "delivered_to": [],
        }

    if isinstance(old, dict):

        if old.get("signature") == signature:

            target_users = old.get(
                "target_users",
                users
            )

            delivered_to = old.get(
                "delivered_to",
                []
            )

            return {
                "signature": signature,
                "target_users": [
                    str(x) for x in target_users
                ],
                "delivered_to": [
                    str(x) for x in delivered_to
                ],
            }

    return {
        "signature": signature,
        "target_users": list(users),
        "delivered_to": [],
    }


# =========================================================
# MAIN
# =========================================================

def main():

    logging.info(
        "Forex V4.4 ishga tushdi"
    )

    # State yuklanmasa, bot davom etmaydi.
    # Bu takroriy signal xavfini kamaytiradi.
    state, state_sha = load_state()

    users = get_users()

    logging.info(
        "Qabul qiluvchilar: %s",
        len(users)
    )

    if not users:
        raise RuntimeError("Qabul qiluvchilar topilmadi")

    cache = {}

    # -----------------------------------------------------
    # Barcha instrumentlar va timeframe ma'lumotlari
    # -----------------------------------------------------

    for symbol in SYMBOLS:

        for tf, interval in TIMEFRAMES.items():

            try:

                cache[(symbol, tf)] = get_candles(
                    symbol,
                    interval
                )

                logging.info(
                    "%s %s: %s candles",
                    symbol,
                    tf,
                    len(cache[(symbol, tf)])
                )

            except Exception as exc:

                logging.warning(
                    "%s %s ma'lumot olinmadi: %s",
                    symbol,
                    tf,
                    exc
                )
                        try:

            cache[(symbol, "D1")] = get_candles(
                symbol,
                "1d"
            )

            logging.info(
                "%s D1: %s candles",
                symbol,
                len(cache[(symbol, "D1")])
            )

        except Exception as exc:

            logging.warning(
                "%s D1 ma'lumot olinmadi: %s",
                symbol,
                exc
            )
    tracking_changed = evaluate_tracked_signals(
        state,
        cache,
        send_telegram
    )
    # -----------------------------------------------------
    # Signal tahlili
    # -----------------------------------------------------

    candidates = []

    for symbol in SYMBOLS:

        h1 = cache.get(
            (symbol, "H1")
        )

        h4 = cache.get(
            (symbol, "H4")
        )

        if not h1 or not h4:

            logging.warning(
                "%s: H1/H4 yo'q, instrument o'tkazildi",
                symbol
            )

            continue

        for tf in TIMEFRAMES:

            candles = cache.get(
                (symbol, tf)
            )

            if not candles:
                continue

            try:

                signal = analyze(
                    symbol,
                    tf,
                    candles,
                    h1,
                    h4
                )

                if signal is None:
                    continue

                key = f"{symbol}_{tf}"

                signature = (
                    f"{signal['candle_time']}_"
                    f"{signal['side']}"
                )

                record = get_delivery_record(
                    state,
                    key,
                    signature,
                    users
                )

                if record is None:
                    continue

                # Allaqachon yuborilgan foydalanuvchilarni
                # qayta yubormaymiz.
                pending_users = [
                    uid
                    for uid in record["target_users"]
                    if uid not in record["delivered_to"]
                ]

                if not pending_users:
                    continue

                candidates.append((
                    key,
                    record,
                    signal,
                    pending_users,
                ))

            except Exception:

                logging.exception(
                    "%s %s tahlil xatosi",
                    symbol,
                    tf
                )

    # Kuchliroq ball olgan signallar birinchi
    candidates.sort(
        key=lambda x: x[2]["score"],
        reverse=True
    )

    # -----------------------------------------------------
    # Telegramga yuborish
    # -----------------------------------------------------

    sent = 0
    state_changed = tracking_changed

    for key, record, signal, pending_users in candidates:

        message = format_signal(signal)

        delivered_now = 0

        for uid in pending_users:

            try:

                send_telegram(
                    uid,
                    message
                )

                if uid not in record["delivered_to"]:

                    record["delivered_to"].append(uid)

                delivered_now += 1

            except Exception:

                logging.exception(
                    "Telegram yuborilmadi: %s",
                    uid
                )

            time.sleep(0.15)

        # Har bir signalning yetkazish holati alohida saqlanadi.
        state["signals"][key] = record
        if register_tracked_signal(state, signal, record):
            state_changed = True
        state_changed = True

        if delivered_now:

            sent += 1

            logging.info(
                "%s %s: %s foydalanuvchiga yuborildi",
                key,
                signal["side"],
                delivered_now
            )

        if len(record["delivered_to"]) < len(
            record["target_users"]
        ):

            logging.warning(
                "%s: ayrim foydalanuvchilarga yuborish kutilmoqda",
                key
            )

    # -----------------------------------------------------
    # STATE SAVE
    # -----------------------------------------------------

    if state_changed:

        state_sha = save_state(
            state,
            state_sha
        )

        logging.info(
            "V4.4 state GitHub'ga saqlandi"
        )

    logging.info(
        "V4.4 yakunlandi. Yangi yuborilgan signallar: %s",
        sent
    )


if __name__ == "__main__":

    main()
