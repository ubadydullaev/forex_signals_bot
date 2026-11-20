
from dataclasses import dataclass
from typing import Optional

from .models import Candle


@dataclass
class SwingPoint:
    index: int
    timestamp: int
    price: float
    kind: str  # HIGH yoki LOW


@dataclass
class StructureEvent:
    event: str  # BOS yoki CHOCH
    direction: str  # BULLISH yoki BEARISH
    price: float
    timestamp: int
    level: float


@dataclass
class LiquiditySweep:
    direction: str  # BULLISH yoki BEARISH
    timestamp: int
    swept_level: float
    close: float


def find_swing_points(
    candles: list[Candle],
    left: int = 3,
    right: int = 3,
) -> list[SwingPoint]:
    """
    Confirmed swing highs and lows.

    A point is confirmed only after `right` candles have closed.
    """

    if left < 1 or right < 1:
        raise ValueError("left and right must be positive")

    points = []

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

    return sorted(points, key=lambda point: point.index)


def detect_trend(
    swing_points: list[SwingPoint],
) -> str:
    """
    Detect market trend using the latest two swing highs
    and latest two swing lows.
    """

    highs = [
        point.price
        for point in swing_points
        if point.kind == "HIGH"
    ]

    lows = [
        point.price
        for point in swing_points
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


def detect_structure_events(
    candles: list[Candle],
    swing_points: list[SwingPoint],
) -> list[StructureEvent]:
    """
    Detect confirmed BOS and CHOCH events.

    A swing level is checked only after its confirmation.
    A candle must close beyond the level.
    """

    events = []

    highs = [
        point
        for point in swing_points
        if point.kind == "HIGH"
    ]

    lows = [
        point
        for point in swing_points
        if point.kind == "LOW"
    ]

    last_broken_high = None
    last_broken_low = None

    current_structure = "UNKNOWN"

    for i, candle in enumerate(candles):

        available_highs = [
            point
            for point in highs
            if point.index + 3 < i
        ]

        available_lows = [
            point
            for point in lows
            if point.index + 3 < i
        ]

        if available_highs:
            candidate = available_highs[-1]

            if (
                last_broken_high is None
                or candidate.index > last_broken_high.index
            ):
                if candle.close > candidate.price:
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
                    last_broken_high = candidate

        if available_lows:
            candidate = available_lows[-1]

            if (
                last_broken_low is None
                or candidate.index > last_broken_low.index
            ):
                if candle.close < candidate.price:
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
                    last_broken_low = candidate

    return events


def detect_liquidity_sweeps(
    candles: list[Candle],
    swing_points: list[SwingPoint],
) -> list[LiquiditySweep]:
    """
    Detect wick sweeps followed by a close back inside the level.
    """

    sweeps = []

    highs = [
        point
        for point in swing_points
        if point.kind == "HIGH"
    ]

    lows = [
        point
        for point in swing_points
        if point.kind == "LOW"
    ]

    for i, candle in enumerate(candles):

        confirmed_highs = [
            point
            for point in highs
            if point.index + 3 < i
        ]

        confirmed_lows = [
            point
            for point in lows
            if point.index + 3 < i
        ]

        if confirmed_highs:
            level = confirmed_highs[-1].price

            if (
                candle.high > level
                and candle.close < level
            ):
                sweeps.append(
                    LiquiditySweep(
                        direction="BEARISH",
                        timestamp=candle.timestamp,
                        swept_level=level,
                        close=candle.close,
                    )
                )

        if confirmed_lows:
            level = confirmed_lows[-1].price

            if (
                candle.low < level
                and candle.close > level
            ):
                sweeps.append(
                    LiquiditySweep(
                        direction="BULLISH",
                        timestamp=candle.timestamp,
                        swept_level=level,
                        close=candle.close,
                    )
                )

    return sweeps


def get_support_resistance(
    candles: list[Candle],
    swing_points: list[SwingPoint],
    current_price: Optional[float] = None,
) -> dict:
    """
    Return the nearest confirmed support and resistance.
    """

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
        if point.kind == "LOW"
        and point.price < current_price
    ]

    resistances = [
        point.price
        for point in swing_points
        if point.kind == "HIGH"
        and point.price > current_price
    ]

    support = max(supports) if supports else None
    resistance = min(resistances) if resistances else None

    return {
        "support": support,
        "resistance": resistance,
    }


def analyze_market_structure(
    candles: list[Candle],
    left: int = 3,
    right: int = 3,
) -> dict:
    """
    Complete market structure analysis.
    """

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
    )

    sweeps = detect_liquidity_sweeps(
        candles,
        swing_points,
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
  
