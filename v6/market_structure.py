
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
# SWING POINTS
# ==========================================

def find_swing_points(
    candles: list[Candle],
    left: int = 3,
    right: int = 3,
) -> list[SwingPoint]:

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


# ==========================================
# TREND DETECTION
# ==========================================

def detect_trend(
    swing_points: list[SwingPoint],
) -> str:

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


# ==========================================
# BOS AND CHOCH
# ==========================================

def detect_structure_events(
    candles: list[Candle],
    swing_points: list[SwingPoint],
    right: int = 3,
) -> list[StructureEvent]:

    if right < 1:
        raise ValueError("right must be positive")

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

    last_broken_high_index = -1
    last_broken_low_index = -1

    current_structure = "UNKNOWN"

    for i, candle in enumerate(candles):

        available_highs = [
            point
            for point in highs
            if point.index + right < i
            and point.index > last_broken_high_index
        ]

        available_lows = [
            point
            for point in lows
            if point.index + right < i
            and point.index > last_broken_low_index
        ]

        # Bullish break of structure
        if available_highs:
            candidate = available_highs[-1]

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
                last_broken_high_index = candidate.index

        # Bearish break of structure
        if available_lows:
            candidate = available_lows[-1]

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
                last_broken_low_index = candidate.index

    return events


# ==========================================
# LIQUIDITY SWEEPS
# ==========================================

def detect_liquidity_sweeps(
    candles: list[Candle],
    swing_points: list[SwingPoint],
    right: int = 3,
) -> list[LiquiditySweep]:

    if right < 1:
        raise ValueError("right must be positive")

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
            if point.index + right < i
        ]

        confirmed_lows = [
            point
            for point in lows
            if point.index + right < i
        ]

        if confirmed_highs:
            level = confirmed_highs[-1].price

            if candle.high > level and candle.close < level:
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

            if candle.low < level and candle.close > level:
                sweeps.append(
                    LiquiditySweep(
                        direction="BULLISH",
                        timestamp=candle.timestamp,
                        swept_level=level,
                        close=candle.close,
                    )
                )

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
        if point.kind == "LOW"
        and point.price < current_price
    ]

    resistances = [
        point.price
        for point in swing_points
        if point.kind == "HIGH"
        and point.price > current_price
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
