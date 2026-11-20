
from typing import Optional

from .models import Candle


# ==========================================
# V6 TECHNICAL INDICATORS
# ==========================================

def ema(values: list[float], period: int) -> list[Optional[float]]:
    """Exponential Moving Average."""

    if period <= 0:
        raise ValueError("Period must be positive")

    result = [None] * len(values)

    if len(values) < period:
        return result

    multiplier = 2.0 / (period + 1)

    current = sum(values[:period]) / period
    result[period - 1] = current

    for i in range(period, len(values)):
        current = (
            values[i] * multiplier
            + current * (1.0 - multiplier)
        )
        result[i] = current

    return result


def rsi(
    closes: list[float],
    period: int = 14
) -> list[Optional[float]]:
    """Wilder Relative Strength Index."""

    if period <= 0:
        raise ValueError("Period must be positive")

    result = [None] * len(closes)

    if len(closes) <= period:
        return result

    gains = []
    losses = []

    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def calculate_rsi(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0

        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    result[period] = calculate_rsi(avg_gain, avg_loss)

    for i in range(period + 1, len(closes)):
        index = i - 1

        avg_gain = (
            avg_gain * (period - 1) + gains[index]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[index]
        ) / period

        result[i] = calculate_rsi(avg_gain, avg_loss)

    return result


def _true_ranges(candles: list[Candle]) -> list[float]:
    """True Range."""

    result = []

    for i, candle in enumerate(candles):
        if i == 0:
            tr = candle.high - candle.low
        else:
            previous_close = candles[i - 1].close

            tr = max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )

        result.append(tr)

    return result


def _wilder_average(
    values: list[Optional[float]],
    period: int
) -> list[Optional[float]]:
    """Wilder smoothing with an initial SMA."""

    result = [None] * len(values)

    valid_indices = []
    initial_values = []

    for i, value in enumerate(values):
        if value is None:
            continue

        valid_indices.append(i)
        initial_values.append(value)

        if len(initial_values) == period:
            break

    if len(initial_values) < period:
        return result

    start_index = valid_indices[-1]
    current = sum(initial_values) / period
    result[start_index] = current

    for i in range(start_index + 1, len(values)):
        value = values[i]

        if value is None:
            continue

        current = (
            current * (period - 1) + value
        ) / period

        result[i] = current

    return result


def atr(
    candles: list[Candle],
    period: int = 14
) -> list[Optional[float]]:
    """Average True Range."""

    if period <= 0:
        raise ValueError("Period must be positive")

    true_ranges = _true_ranges(candles)

    return _wilder_average(
        true_ranges,
        period
    )


def macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
):
    """Moving Average Convergence Divergence."""

    if fast <= 0 or slow <= 0 or signal_period <= 0:
        raise ValueError("Periods must be positive")

    if fast >= slow:
        raise ValueError("Fast period must be smaller than slow period")

    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)

    macd_line = [
        (
            fast_ema[i] - slow_ema[i]
            if fast_ema[i] is not None
            and slow_ema[i] is not None
            else None
        )
        for i in range(len(closes))
    ]

    signal_line = _wilder_ema_optional(
        macd_line,
        signal_period
    )

    histogram = [
        (
            macd_line[i] - signal_line[i]
            if macd_line[i] is not None
            and signal_line[i] is not None
            else None
        )
        for i in range(len(closes))
    ]

    return {
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram,
    }


def _wilder_ema_optional(
    values: list[Optional[float]],
    period: int
) -> list[Optional[float]]:
    """EMA for a series containing None values."""

    result = [None] * len(values)

    valid = [
        (i, value)
        for i, value in enumerate(values)
        if value is not None
    ]

    if len(valid) < period:
        return result

    multiplier = 2.0 / (period + 1)

    start_index = valid[period - 1][0]
    current = sum(
        value for _, value in valid[:period]
    ) / period

    result[start_index] = current

    for i in range(start_index + 1, len(values)):
        value = values[i]

        if value is None:
            continue

        current = (
            value * multiplier
            + current * (1.0 - multiplier)
        )

        result[i] = current

    return result


def adx(
    candles: list[Candle],
    period: int = 14
):
    """Average Directional Index."""

    if period <= 0:
        raise ValueError("Period must be positive")

    length = len(candles)

    plus_dm = [0.0] * length
    minus_dm = [0.0] * length

    for i in range(1, length):
        up_move = (
            candles[i].high - candles[i - 1].high
        )

        down_move = (
            candles[i - 1].low - candles[i].low
        )

        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move

        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move

    tr = _true_ranges(candles)

    smooth_tr = _wilder_average(tr, period)
    smooth_plus = _wilder_average(plus_dm, period)
    smooth_minus = _wilder_average(minus_dm, period)

    plus_di = [None] * length
    minus_di = [None] * length
    dx = [None] * length

    for i in range(length):
        if (
            smooth_tr[i] is None
            or smooth_tr[i] == 0
            or smooth_plus[i] is None
            or smooth_minus[i] is None
        ):
            continue

        plus_di[i] = (
            100.0 * smooth_plus[i] / smooth_tr[i]
        )

        minus_di[i] = (
            100.0 * smooth_minus[i] / smooth_tr[i]
        )

        denominator = plus_di[i] + minus_di[i]

        if denominator != 0:
            dx[i] = (
                100.0
                * abs(plus_di[i] - minus_di[i])
                / denominator
            )

    adx_values = _wilder_average(dx, period)

    return {
        "adx": adx_values,
        "plus_di": plus_di,
        "minus_di": minus_di,
    }


def vwap(
    candles: list[Candle]
) -> list[Optional[float]]:
    """
    Cumulative VWAP over the supplied candles.

    Note: This does not automatically reset at exchange sessions.
    """

    result = []

    cumulative_volume = 0.0
    cumulative_value = 0.0

    for candle in candles:
        typical_price = (
            candle.high
            + candle.low
            + candle.close
        ) / 3.0

        volume = max(candle.volume, 0.0)

        cumulative_value += typical_price * volume
        cumulative_volume += volume

        if cumulative_volume == 0:
            result.append(None)
        else:
            result.append(
                cumulative_value / cumulative_volume
            )

    return result


def supertrend(
    candles: list[Candle],
    period: int = 10,
    multiplier: float = 3.0,
):
    """Supertrend using Wilder ATR."""

    if period <= 0 or multiplier <= 0:
        raise ValueError("Invalid Supertrend settings")

    length = len(candles)
    atr_values = atr(candles, period)

    upper_band = [None] * length
    lower_band = [None] * length
    trend = [None] * length
    direction = [None] * length

    for i in range(length):
        current_atr = atr_values[i]

        if current_atr is None:
            continue

        midpoint = (
            candles[i].high + candles[i].low
        ) / 2.0

        basic_upper = midpoint + multiplier * current_atr
        basic_lower = midpoint - multiplier * current_atr

        if i == 0 or upper_band[i - 1] is None:
            upper_band[i] = basic_upper
            lower_band[i] = basic_lower
            direction[i] = 1
            trend[i] = basic_lower
            continue

        previous_close = candles[i - 1].close

        if (
            basic_upper < upper_band[i - 1]
            or previous_close > upper_band[i - 1]
        ):
            upper_band[i] = basic_upper
        else:
            upper_band[i] = upper_band[i - 1]

        if (
            basic_lower > lower_band[i - 1]
            or previous_close < lower_band[i - 1]
        ):
            lower_band[i] = basic_lower
        else:
            lower_band[i] = lower_band[i - 1]

        if direction[i - 1] == 1:
            direction[i] = (
                -1
                if candles[i].close < lower_band[i]
                else 1
            )
        else:
            direction[i] = (
                1
                if candles[i].close > upper_band[i]
                else -1
            )

        trend[i] = (
            lower_band[i]
            if direction[i] == 1
            else upper_band[i]
        )

    return {
        "supertrend": trend,
        "direction": direction,
        "upper_band": upper_band,
        "lower_band": lower_band,
    }


def calculate_indicators(
    candles: list[Candle]
) -> dict:
    """Calculate all V6 indicators."""

    closes = [candle.close for candle in candles]

    macd_result = macd(closes)
    adx_result = adx(candles)
    supertrend_result = supertrend(candles)

    return {
        "ema_20": ema(closes, 20),
        "ema_50": ema(closes, 50),
        "ema_200": ema(closes, 200),
        "rsi_14": rsi(closes, 14),
        "atr_14": atr(candles, 14),
        "macd": macd_result,
        "adx": adx_result,
        "vwap": vwap(candles),
        "supertrend": supertrend_result,
      }
      
