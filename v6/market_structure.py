
from dataclasses import dataclass
from typing import Optional

from .models import Candle


# ==========================================
# V6 MARKET STRUCTURE
# ==========================================

@dataclass
class SwingPoint:
    index: int
    timestamp: int
    price: float
    kind: str


@dataclass
class StructureEvent:
    event: str
    direction: str
    price: float
    timestamp: int
    level: float


@dataclass
class LiquiditySweep:
    direction: str
    timestamp: int
    swept_level: float
    close: float


# ==========================================
# INTERNAL HELPERS
# ==========================================

def _validate_window(left: int = 1, right: int = 1) -> None:
    if (
        isinstance(left, bool)
        or not isinstance(left, int)
        or left < 1
    ):
        raise ValueError("left must be a positive integer")

    if (
        isinstance(right, bool)
        or not isinstance(right, int)
        or right < 1
    ):
        raise ValueError("right must be a positive integer")


def _valid_swing_points(
    swing_points: list[SwingPoint],
    candle_count: int,
) -> list[SwingPoint]:
    """Keep structurally valid swing points and sort chronologically."""

    valid = []

    for point in swing_points:
        if point.kind not in ("HIGH", "LOW"):
            continue

        if (
            isinstance(point.index, bool)
            or not isinstance(point.index, int)
            or point.index < 0
            or point.index >= candle_count
        ):
            continue

        if (
            not isinstance(point.price, (int, float))
            or isinstance(point.price, bool)
        ):
            continue

        if point.price <= 0:
            continue

        valid.append(point)

    return sorted(
        valid,
        key=lambda point: (point.index, point.timestamp),
    )


def _confirmed_before(
    point: SwingPoint,
    candle_index: int,
    right: int,
) -> bool:
    """
    A swing becomes usable only after its right-side confirmation
    candles have closed. It is first usable on the following candle.
    """

    return point.index + right < candle_index


# ==========================================
# SWING POINTS
# ==========================================

def find_swing_points(
    candles: list[Candle],
    left: int = 3,
    right: int = 3,
) -> list[SwingPoint]:

    _validate_window(left, right)

    points = []

    if len(candles) < left + right + 1:
        return points

    for i in range(left, len(candles) - right):
        current = candles[i]

        left_candles = candles[i - left:i]
        right_candles = candles[i + 1:i + right + 1]

        is_high = all(
            current.high > candle.high
            for candle in left_candles + right_candles
        )

        is_low = all(
            current.low < candle.low
            for candle in left_candles + right_candles
        )

        if is_high:
            points.append(
                SwingPoint(
                    index=i,
                    timestamp=current.timestamp,
                    price=current.high,
                    kind="HIGH",
                )
            )

        if is_low:
            points.append(
                SwingPoint(
                    index=i,
                    timestamp=current.timestamp,
                    price=current.low,
                    kind="LOW",
                )
            )

    return sorted(
        points,
        key=lambda point: (point.index, point.timestamp),
    )


# ==========================================
# TREND DETECTION
# ==========================================

def detect_trend(
    swing_points: list[SwingPoint],
) -> str:

    highs = [
        point.price
        for point in sorted(
            swing_points,
            key=lambda point: point.index,
        )
        if point.kind == "HIGH"
    ]

    lows = [
        point.price
        for point in sorted(
            swing_points,
            key=lambda point: point.index,
        )
        if point.kind == "LOW"
    ]

    if len(highs) < 2 or len(lows) < 2:
        return "UNKNOWN"

    higher_high = highs[-1] > highs[-2]
    higher_low = lows[-1] > lows[-2]

    lower_high = highs[-1] < highs[-2]
    lower_low = lows[-1] < lows[-2]

    if higher_high and higher_low:
        return "BULLISH"

    if lower_high and lower_low:
        return "BEARISH"

    return "RANGING"


# ==========================================
# BOS AND CHOCH
# ==========================================

def detect_structure_events(
    candles: list[Candle],
    swing_points: list[SwingPoint],
    right: int = 3,
) -> list[StructureEvent]:

    _validate_window(right=right)

    if len(candles) < 2 or not swing_points:
        return []

    points = _valid_swing_points(
        swing_points,
        len(candles),
    )

    highs = [
        point for point in points
        if point.kind == "HIGH"
    ]

    lows = [
        point for point in points
        if point.kind == "LOW"
    ]

    events = []

    broken_highs = set()
    broken_lows = set()

    current_structure = "UNKNOWN"

    for i in range(1, len(candles)):
        candle = candles[i]
        previous_close = candles[i - 1].close

        available_highs = [
            point
            for point in highs
            if (
                point.index not in broken_highs
                and _confirmed_before(point, i, right)
            )
        ]

        available_lows = [
            point
            for point in lows
            if (
                point.index not in broken_lows
                and _confirmed_before(point, i, right)
            )
        ]

        # ----------------------------------
        # BULLISH BREAK
        # ----------------------------------

        crossed_highs = [
            point
            for point in available_highs
            if (
                previous_close <= point.price
                and candle.close > point.price
            )
        ]

        # Levels already crossed before becoming usable
        # are invalidated without generating a late event.
        for point in available_highs:
            if (
                previous_close > point.price
                and candle.close > point.price
            ):
                broken_highs.add(point.index)

        if crossed_highs:
            candidate = crossed_highs[-1]

            event_type = (
                "CHOCH"
                if current_structure == "BEARISH"
                else "BOS"
            )

            events.append(
                StructureEvent(
                    event=event_type,
                    direction="BULLISH",
                    price=candle.close,
                    timestamp=candle.timestamp,
                    level=candidate.price,
                )
            )

            current_structure = "BULLISH"

            # Consume every high level crossed by this candle.
            for point in crossed_highs:
                broken_highs.add(point.index)

        # ----------------------------------
        # BEARISH BREAK
        # ----------------------------------

        crossed_lows = [
            point
            for point in available_lows
            if (
                previous_close >= point.price
                and candle.close < point.price
            )
        ]

        for point in available_lows:
            if (
                previous_close < point.price
                and candle.close < point.price
            ):
                broken_lows.add(point.index)

        if crossed_lows:
            candidate = crossed_lows[-1]

            event_type = (
                "CHOCH"
                if current_structure == "BULLISH"
                else "BOS"
            )

            events.append(
                StructureEvent(
                    event=event_type,
                    direction="BEARISH",
                    price=candle.close,
                    timestamp=candle.timestamp,
                    level=candidate.price,
                )
            )

            current_structure = "BEARISH"

            for point in crossed_lows:
                broken_lows.add(point.index)

    return events


# ==========================================
# LIQUIDITY SWEEPS
# ==========================================

def detect_liquidity_sweeps(
    candles: list[Candle],
    swing_points: list[SwingPoint],
    right: int = 3,
) -> list[LiquiditySweep]:

    _validate_window(right=right)

    if not candles or not swing_points:
        return []

    points = _valid_swing_points(
        swing_points,
        len(candles),
    )

    highs = [
        point for point in points
        if point.kind == "HIGH"
    ]

    lows = [
        point for point in points
        if point.kind == "LOW"
    ]

    sweeps = []

    consumed_highs = set()
    consumed_lows = set()

    for i, candle in enumerate(candles):

        confirmed_highs = [
            point
            for point in highs
            if (
                point.index not in consumed_highs
                and _confirmed_before(point, i, right)
            )
        ]

        confirmed_lows = [
            point
            for point in lows
            if (
                point.index not in consumed_lows
                and _confirmed_before(point, i, right)
            )
        ]

        # ----------------------------------
        # BEARISH LIQUIDITY SWEEP
        # Price takes a previous high and
        # closes back below that level.
        # ----------------------------------

        swept_highs = [
            point
            for point in confirmed_highs
            if (
                candle.high > point.price
                and candle.close < point.price
            )
        ]

        if swept_highs:
            candidate = swept_highs[-1]

            sweeps.append(
                LiquiditySweep(
                    direction="BEARISH",
                    timestamp=candle.timestamp,
                    swept_level=candidate.price,
                    close=candle.close,
                )
            )

            # A swept level must not be counted again.
            for point in swept_highs:
                consumed_highs.add(point.index)

        # A close above a high invalidates that old liquidity level.
        for point in confirmed_highs:
            if candle.close > point.price:
                consumed_highs.add(point.index)

        # ----------------------------------
        # BULLISH LIQUIDITY SWEEP
        # Price takes a previous low and
        # closes back above that level.
        # ----------------------------------

        swept_lows = [
            point
            for point in confirmed_lows
            if (
                candle.low < point.price
                and candle.close > point.price
            )
        ]

        if swept_lows:
            candidate = swept_lows[-1]

            sweeps.append(
                LiquiditySweep(
                    direction="BULLISH",
                    timestamp=candle.timestamp,
                    swept_level=candidate.price,
                    close=candle.close,
                )
            )

            for point in swept_lows:
                consumed_lows.add(point.index)

        # A close below a low invalidates that old liquidity level.
        for point in confirmed_lows:
            if candle.close < point.price:
                consumed_lows.add(point.index)

    return sweeps


# ==========================================
# SUPPORT AND RESISTANCE
# ==========================================

def get_support_resistance(
    candles: list[Candle],
    swing_points: list[SwingPoint],
    current_price: Optional[float] = None,
) -> dict:

    if current_price is None and candles:
        current_price = candles[-1].close

    if current_price is None:
        return {
            "support": None,
            "resistance": None,
        }

    supports = [
        point.price
        for point in swing_points
        if (
            point.kind == "LOW"
            and point.price < current_price
        )
    ]

    resistances = [
        point.price
        for point in swing_points
        if (
            point.kind == "HIGH"
            and point.price > current_price
        )
    ]

    return {
        "support": max(supports) if supports else None,
        "resistance": min(resistances) if resistances else None,
    }


# ==========================================
# COMPLETE ANALYSIS
# ==========================================

def analyze_market_structure(
    candles: list[Candle],
    left: int = 3,
    right: int = 3,
) -> dict:

    _validate_window(left, right)

    if not candles:
        return {
            "trend": "UNKNOWN",
            "swings": [],
            "events": [],
            "liquidity_sweeps": [],
            "levels": {
                "support": None,
                "resistance": None,
            },
        }

    swing_points = find_swing_points(
        candles,
        left=left,
        right=right,
    )

    trend = detect_trend(swing_points)

    events = detect_structure_events(
        candles,
        swing_points,
        right=right,
    )

    sweeps = detect_liquidity_sweeps(
        candles,
        swing_points,
        right=right,
    )

    levels = get_support_resistance(
        candles,
        swing_points,
    )

    return {
        "trend": trend,
        "swings": swing_points,
        "events": events,
        "liquidity_sweeps": sweeps,
        "levels": levels,
    }
