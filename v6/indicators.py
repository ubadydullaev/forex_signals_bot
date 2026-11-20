
from __future__ import annotations

from math import isfinite
from typing import Optional, Sequence, Any

from .models import Candle


# ==========================================
# V6 TECHNICAL INDICATORS
# ==========================================

Number = int | float
OptionalFloat = Optional[float]


def _validate_period(period: int) -> None:
    if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
        raise ValueError("Period must be a positive integer")


def _validate_number(value: Number, name: str) -> float:
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def ema(
    values: Sequence[Optional[Number]],
    period: int,
) -> list[OptionalFloat]:
    """
    Exponential Moving Average.

    Uses the first valid source value as its initial seed,
    matching the usual TradingView ta.ema initialization.
    """

    _validate_period(period)

    result: list[OptionalFloat] = [None] * len(values)
    alpha = 2.0 / (period + 1.0)
    current: OptionalFloat = None

    for i, value in enumerate(values):
        if value is None:
            continue

        source = _validate_number(value, "EMA source")

        if current is None:
            current = source
        else:
            current = alpha * source + (1.0 - alpha) * current

        result[i] = current

    return result


def _wilder_average(
    values: Sequence[Optional[Number]],
    period: int,
) -> list[OptionalFloat]:
    """
    Wilder's Moving Average (RMA).

    Initialization: SMA of the first `period` valid values.
    Following values: (previous * (period - 1) + current) / period.
    """

    _validate_period(period)

    result: list[OptionalFloat] = [None] * len(values)
    initial: list[float] = []
    current: OptionalFloat = None

    for i, value in enumerate(values):
        if value is None:
            continue

        initial.append(_validate_number(value, "RMA source"))

        if len(initial) == period:
            current = sum(initial) / period
            result[i] = current
            break

    if current is None:
        return result

    for i in range(i + 1, len(values)):
        value = values[i]

        if value is None:
            continue

        source = _validate_number(value, "RMA source")
        current = (
            current * (period - 1) + source
        ) / period
        result[i] = current

    return result


def rsi(
    closes: Sequence[Number],
    period: int = 14,
) -> list[OptionalFloat]:
    """Relative Strength Index using Wilder RMA."""

    _validate_period(period)

    prices = [
        _validate_number(value, "RSI close")
        for value in closes
    ]

    result: list[OptionalFloat] = [None] * len(prices)

    if len(prices) <= period:
        return result

    gains: list[OptionalFloat] = [None]
    losses: list[OptionalFloat] = [None]

    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    average_gain = _wilder_average(gains, period)
    average_loss = _wilder_average(losses, period)

    for i, (gain, loss) in enumerate(
        zip(average_gain, average_loss)
    ):
        if gain is None or loss is None:
            continue

        if gain == 0.0 and loss == 0.0:
            result[i] = 50.0
        elif loss == 0.0:
            result[i] = 100.0
        elif gain == 0.0:
            result[i] = 0.0
        else:
            relative_strength = gain / loss
            result[i] = (
                100.0 - 100.0 / (1.0 + relative_strength)
            )

    return result


def _true_ranges(candles: Sequence[Candle]) -> list[float]:
    """True Range, using high-low for the first candle."""

    result: list[float] = []

    for i, candle in enumerate(candles):
        high = _validate_number(candle.high, "Candle high")
        low = _validate_number(candle.low, "Candle low")

        if high < low:
            raise ValueError("Candle high cannot be below low")

        if i == 0:
            true_range = high - low
        else:
            previous_close = _validate_number(
                candles[i - 1].close,
                "Previous close",
            )

            true_range = max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )

        result.append(true_range)

    return result


def atr(
    candles: Sequence[Candle],
    period: int = 14,
) -> list[OptionalFloat]:
    """Average True Range using Wilder RMA."""

    _validate_period(period)

    return _wilder_average(
        _true_ranges(candles),
        period,
    )


def macd(
    closes: Sequence[Number],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> dict[str, list[OptionalFloat]]:
    """
    Moving Average Convergence Divergence.

    MACD line = fast EMA - slow EMA
    Signal line = EMA of MACD line
    Histogram = MACD line - signal line
    """

    _validate_period(fast)
    _validate_period(slow)
    _validate_period(signal_period)

    if fast >= slow:
        raise ValueError("Fast period must be smaller than slow period")

    prices = [
        _validate_number(value, "MACD close")
        for value in closes
    ]

    fast_values = ema(prices, fast)
    slow_values = ema(prices, slow)

    macd_line: list[OptionalFloat] = []

    for fast_value, slow_value in zip(fast_values, slow_values):
        if fast_value is None or slow_value is None:
            macd_line.append(None)
        else:
            macd_line.append(fast_value - slow_value)

    signal_line = _wilder_ema(macd_line, signal_period)

    histogram: list[OptionalFloat] = []

    for main, signal in zip(macd_line, signal_line):
        if main is None or signal is None:
            histogram.append(None)
        else:
            histogram.append(main - signal)

    return {
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram,
    }


def _wilder_ema(
    values: Sequence[OptionalFloat],
    period: int,
) -> list[OptionalFloat]:
    """EMA for MACD signal calculation."""

    _validate_period(period)

    result: list[OptionalFloat] = [None] * len(values)
    alpha = 2.0 / (period + 1.0)
    current: OptionalFloat = None

    for i, value in enumerate(values):
        if value is None:
            continue

        source = _validate_number(value, "EMA source")

        if current is None:
            current = source
        else:
            current = alpha * source + (1.0 - alpha) * current

        result[i] = current

    return result


def adx(
    candles: Sequence[Candle],
    period: int = 14,
) -> dict[str, list[OptionalFloat]]:
    """
    Directional Movement Index and ADX.

    +DI and -DI use Wilder-smoothed directional movement
    divided by Wilder-smoothed True Range.
    ADX is the Wilder average of DX.
    """

    _validate_period(period)

    length = len(candles)

    plus_dm: list[OptionalFloat] = [None] * length
    minus_dm: list[OptionalFloat] = [None] * length

    for i in range(1, length):
        current_high = _validate_number(
            candles[i].high, "Candle high"
        )
        previous_high = _validate_number(
            candles[i - 1].high, "Previous high"
        )
        current_low = _validate_number(
            candles[i].low, "Candle low"
        )
        previous_low = _validate_number(
            candles[i - 1].low, "Previous low"
        )

        upward_move = current_high - previous_high
        downward_move = previous_low - current_low

        plus_dm[i] = (
            upward_move
            if upward_move > downward_move and upward_move > 0
            else 0.0
        )

        minus_dm[i] = (
            downward_move
            if downward_move > upward_move and downward_move > 0
            else 0.0
        )

    smoothed_tr = _wilder_average(
        _true_ranges(candles),
        period,
    )
    smoothed_plus = _wilder_average(plus_dm, period)
    smoothed_minus = _wilder_average(minus_dm, period)

    plus_di: list[OptionalFloat] = [None] * length
    minus_di: list[OptionalFloat] = [None] * length
    dx: list[OptionalFloat] = [None] * length

    for i in range(length):
        tr_value = smoothed_tr[i]
        plus_value = smoothed_plus[i]
        minus_value = smoothed_minus[i]

        if (
            tr_value is None
            or plus_value is None
            or minus_value is None
        ):
            continue

        if tr_value == 0.0:
            plus_di[i] = 0.0
            minus_di[i] = 0.0
        else:
            plus_di[i] = 100.0 * plus_value / tr_value
            minus_di[i] = 100.0 * minus_value / tr_value

        denominator = plus_di[i] + minus_di[i]

        if denominator == 0.0:
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
    candles: Sequence[Candle],
    session_keys: Optional[Sequence[Any]] = None,
) -> list[OptionalFloat]:
    """
    Volume Weighted Average Price.

    By default, calculates cumulative VWAP across supplied candles.

    To reset at session boundaries, pass one session key per candle.
    Example: a date string or market-session identifier.

    The correct session key depends on the instrument and exchange.
    """

    if session_keys is not None and len(session_keys) != len(candles):
        raise ValueError("session_keys length must match candles length")

    result: list[OptionalFloat] = []

    cumulative_volume = 0.0
    cumulative_value = 0.0
    previous_session: Any = object()

    for i, candle in enumerate(candles):
        if session_keys is not None:
            current_session = session_keys[i]

            if i == 0 or current_session != previous_session:
                cumulative_volume = 0.0
                cumulative_value = 0.0

            previous_session = current_session

        high = _validate_number(candle.high, "VWAP high")
        low = _validate_number(candle.low, "VWAP low")
        close = _validate_number(candle.close, "VWAP close")
        volume = _validate_number(candle.volume, "VWAP volume")

        if volume < 0.0:
            raise ValueError("VWAP volume cannot be negative")

        typical_price = (high + low + close) / 3.0

        cumulative_value += typical_price * volume
        cumulative_volume += volume

        if cumulative_volume == 0.0:
            result.append(None)
        else:
            result.append(cumulative_value / cumulative_volume)

    return result


def supertrend(
    candles: Sequence[Candle],
    period: int = 10,
    multiplier: float = 3.0,
) -> dict[str, list]:
    """
    Supertrend using Wilder ATR.

    Internal direction convention:
        1  = BULLISH
        -1 = BEARISH

    Note: this internal convention is intentionally preserved
    for compatibility with the V6 signal engine.
    """

    _validate_period(period)
    multiplier = _validate_number(multiplier, "Supertrend multiplier")

    if multiplier <= 0.0:
        raise ValueError("Supertrend multiplier must be positive")

    length = len(candles)
    atr_values = atr(candles, period)

    upper_band: list[OptionalFloat] = [None] * length
    lower_band: list[OptionalFloat] = [None] * length
    trend: list[OptionalFloat] = [None] * length
    direction: list[Optional[int]] = [None] * length

    for i in range(length):
        current_atr = atr_values[i]

        if current_atr is None:
            continue

        high = _validate_number(candles[i].high, "Supertrend high")
        low = _validate_number(candles[i].low, "Supertrend low")
        close = _validate_number(candles[i].close, "Supertrend close")

        midpoint = (high + low) / 2.0
        basic_upper = midpoint + multiplier * current_atr
        basic_lower = midpoint - multiplier * current_atr

        previous_index = i - 1

        if (
            i == 0
            or upper_band[previous_index] is None
            or lower_band[previous_index] is None
            or direction[previous_index] is None
        ):
            upper_band[i] = basic_upper
            lower_band[i] = basic_lower
            direction[i] = -1
            trend[i] = basic_upper
            continue

        previous_close = _validate_number(
            candles[previous_index].close,
            "Previous close",
        )

        previous_upper = upper_band[previous_index]
        previous_lower = lower_band[previous_index]

        upper_band[i] = (
            basic_upper
            if basic_upper < previous_upper
            or previous_close > previous_upper
            else previous_upper
        )

        lower_band[i] = (
            basic_lower
            if basic_lower > previous_lower
            or previous_close < previous_lower
            else previous_lower
        )

        previous_direction = direction[previous_index]

        if previous_direction == -1:
            direction[i] = (
                1 if close > upper_band[i] else -1
            )
        else:
            direction[i] = (
                -1 if close < lower_band[i] else 1
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
    candles: Sequence[Candle],
    vwap_session_keys: Optional[Sequence[Any]] = None,
) -> dict:
    """Calculate the complete V6 indicator set."""

    closes = [
        _validate_number(candle.close, "Indicator close")
        for candle in candles
    ]

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
        "vwap": vwap(candles, vwap_session_keys),
        "supertrend": supertrend_result,
    }
