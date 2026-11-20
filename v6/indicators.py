
from typing import Optional

from .models import Candle


# ==========================================
# V6 TECHNICAL INDICATORS
# ==========================================


def ema(
    values: list[float],
    period: int,
) -> list[Optional[float]]:
    """EMA using the first valid source value as its seed."""

    if period <= 0:
        raise ValueError("Period must be positive")

    result: list[Optional[float]] = [None] * len(values)

    if not values:
        return result

    multiplier = 2.0 / (period + 1)
    current = float(values[0])
    result[0] = current

    for i in range(1, len(values)):
        current = (
            float(values[i]) * multiplier
            + current * (1.0 - multiplier)
        )
        result[i] = current

    return result


def _wilder_average(
    values: list[Optional[float]],
    period: int,
) -> list[Optional[float]]:
    """Wilder RMA with an SMA seed from the first period valid values."""

    if period <= 0:
        raise ValueError("Period must be positive")

    result: list[Optional[float]] = [None] * len(values)

    initial: list[float] = []
    start_index = None

    for i, value in enumerate(values):
        if value is None:
            continue

        initial.append(float(value))

        if len(initial) == period:
            start_index = i
            break

    if start_index is None:
        return result

    current = sum(initial) / period
    result[start_index] = current

    for i in range(start_index + 1, len(values)):
        value = values[i]

        if value is None:
            continue

        current = (
            current * (period - 1) + float(value)
        ) / period

        result[i] = current

    return result


def rsi(
    closes: list[float],
    period: int = 14,
) -> list[Optional[float]]:
    """Relative Strength Index using Wilder smoothing."""

    if period <= 0:
        raise ValueError("Period must be positive")

    result: list[Optional[float]] = [None] * len(closes)

    if len(closes) <= period:
        return result

    gains: list[Optional[float]] = [None]
    losses: list[Optional[float]] = [None]

    for i in range(1, len(closes)):
        change = float(closes[i]) - float(closes[i - 1])
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = _wilder_average(gains, period)
    avg_loss = _wilder_average(losses, period)

    for i in range(len(closes)):
        gain = avg_gain[i]
        loss = avg_loss[i]

        if gain is None or loss is None:
            continue

        if loss == 0:
            result[i] = 100.0 if gain > 0 else 50.0
        elif gain == 0:
            result[i] = 0.0
        else:
            rs = gain / loss
            result[i] = 100.0 - (100.0 / (1.0 + rs))

    return result


def _true_ranges(
    candles: list[Candle],
) -> list[float]:
    """True Range."""

    result: list[float] = []

    for i, candle in enumerate(candles):
        if i == 0:
            value = candle.high - candle.low
        else:
            previous_close = candles[i - 1].close

            value = max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )

        result.append(value)

    return result


def atr(
    candles: list[Candle],
    period: int = 14,
) -> list[Optional[float]]:
    """Average True Range using Wilder RMA."""

    if period <= 0:
        raise ValueError("Period must be positive")

    return _wilder_average(
        _true_ranges(candles),
        period,
    )


def _ema_optional(
    values: list[Optional[float]],
    period: int,
) -> list[Optional[float]]:
    """EMA for a series containing None values."""

    if period <= 0:
        raise ValueError("Period must be positive")

    result: list[Optional[float]] = [None] * len(values)

    multiplier = 2.0 / (period + 1)
    current = None

    for i, value in enumerate(values):
        if value is None:
            continue

        if current is None:
            current = float(value)
        else:
            current = (
                float(value) * multiplier
                + current * (1.0 - multiplier)
            )

        result[i] = current

    return result


def macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> dict:
    """MACD with EMA-based fast, slow and signal lines."""

    if fast <= 0 or slow <= 0 or signal_period <= 0:
        raise ValueError("Periods must be positive")

    if fast >= slow:
        raise ValueError("Fast period must be smaller than slow period")

    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)

    macd_line: list[Optional[float]] = []

    for i in range(len(closes)):
        fast_value = fast_ema[i]
        slow_value = slow_ema[i]

        if fast_value is None or slow_value is None:
            macd_line.append(None)
        else:
            macd_line.append(fast_value - slow_value)

    signal_line = _ema_optional(
        macd_line,
        signal_period,
    )

    histogram: list[Optional[float]] = []

    for i in range(len(closes)):
        main = macd_line[i]
        signal = signal_line[i]

        if main is None or signal is None:
            histogram.append(None)
        else:
            histogram.append(main - signal)

    return {
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram,
    }


def adx(
    candles: list[Candle],
    period: int = 14,
) -> dict:
    """Directional Movement Index and ADX using Wilder smoothing."""

    if period <= 0:
        raise ValueError("Period must be positive")

    length = len(candles)

    plus_dm: list[Optional[float]] = [None] * length
    minus_dm: list[Optional[float]] = [None] * length

    for i in range(1, length):
        up_move = (
            candles[i].high - candles[i - 1].high
        )

        down_move = (
            candles[i - 1].low - candles[i].low
        )

        plus_dm[i] = (
            up_move
            if up_move > down_move and up_move > 0
            else 0.0
        )

        minus_dm[i] = (
            down_move
            if down_move > up_move and down_move > 0
            else 0.0
        )

    smooth_tr = _wilder_average(
        _true_ranges(candles),
        period,
    )

    smooth_plus = _wilder_average(plus_dm, period)
    smooth_minus = _wilder_average(minus_dm, period)

    plus_di: list[Optional[float]] = [None] * length
    minus_di: list[Optional[float]] = [None] * length
    dx: list[Optional[float]] = [None] * length

    for i in range(length):
        tr_value = smooth_tr[i]
        plus_value = smooth_plus[i]
        minus_value = smooth_minus[i]

        if (
            tr_value is None
            or tr_value <= 0
            or plus_value is None
            or minus_value is None
        ):
            continue

        plus_di[i] = 100.0 * plus_value / tr_value
        minus_di[i] = 100.0 * minus_value / tr_value

        denominator = plus_di[i] + minus_di[i]

        if denominator == 0:
            dx[i] = 0.0
        else:
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
    candles: list[Candle],
) -> list[Optional[float]]:
    """
    Cumulative VWAP over supplied candles.

    This function does not reset at exchange sessions.
    Session anchoring requires a separate market-specific rule.
    """

    result: list[Optional[float]] = []

    cumulative_volume = 0.0
    cumulative_value = 0.0

    for candle in candles:
        typical_price = (
            candle.high
            + candle.low
            + candle.close
        ) / 3.0

        volume = max(float(candle.volume), 0.0)

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
) -> dict:
    """
    Supertrend using Wilder ATR.

    Internal direction convention:
        1  = BULLISH
        -1 = BEARISH
    """

    if period <= 0 or multiplier <= 0:
        raise ValueError("Invalid Supertrend settings")

    length = len(candles)
    atr_values = atr(candles, period)

    upper_band: list[Optional[float]] = [None] * length
    lower_band: list[Optional[float]] = [None] * length
    trend: list[Optional[float]] = [None] * length
    direction: list[Optional[int]] = [None] * length

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

            # Pine's initial direction is bearish.
            # Convert it to this engine's internal convention.
            direction[i] = -1
            trend[i] = basic_upper
            continue

        previous_close = candles[i - 1].close

        previous_upper = upper_band[i - 1]
        previous_lower = lower_band[i - 1]

        if previous_upper is None or previous_lower is None:
            continue

        if (
            basic_upper < previous_upper
            or previous_close > previous_upper
        ):
            upper_band[i] = basic_upper
        else:
            upper_band[i] = previous_upper

        if (
            basic_lower > previous_lower
            or previous_close < previous_lower
        ):
            lower_band[i] = basic_lower
        else:
            lower_band[i] = previous_lower

        previous_direction = direction[i - 1]

        if previous_direction is None:
            continue

        if previous_direction == -1:
            direction[i] = (
                1
                if candles[i].close > upper_band[i]
                else -1
            )
        else:
            direction[i] = (
                -1
                if candles[i].close < lower_band[i]
                else 1
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
    candles: list[Candle],
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
