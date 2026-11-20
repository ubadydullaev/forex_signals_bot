
from dataclasses import dataclass
from typing import Optional


@dataclass
class ValidationResult:
    valid: bool
    reasons: list[str]


def validate_market_data(
    candles,
    minimum_candles: int = 200,
) -> ValidationResult:
    """Check whether enough valid candle data is available."""

    reasons = []

    if not candles:
        return ValidationResult(
            valid=False,
            reasons=["Candle data is empty"],
        )

    if len(candles) < minimum_candles:
        reasons.append(
            f"Not enough candles: {len(candles)}/{minimum_candles}"
        )

    for i, candle in enumerate(candles):
        if candle.high < candle.low:
            reasons.append(f"Invalid high/low at candle {i}")
            break

        if candle.open <= 0 or candle.close <= 0:
            reasons.append(f"Invalid price at candle {i}")
            break

        if candle.volume < 0:
            reasons.append(f"Invalid volume at candle {i}")
            break

    timestamps = [c.timestamp for c in candles]

    if timestamps != sorted(timestamps):
        reasons.append("Candle timestamps are not chronological")

    if len(timestamps) != len(set(timestamps)):
        reasons.append("Duplicate candle timestamps found")

    return ValidationResult(
        valid=len(reasons) == 0,
        reasons=reasons,
    )


def validate_stop_loss(
    side: str,
    entry: float,
    stop_loss: float,
) -> ValidationResult:
    """Check stop-loss placement."""

    if entry <= 0 or stop_loss <= 0:
        return ValidationResult(False, ["Prices must be positive"])

    if side == "BUY" and stop_loss >= entry:
        return ValidationResult(
            False,
            ["BUY stop-loss must be below entry"],
        )

    if side == "SELL" and stop_loss <= entry:
        return ValidationResult(
            False,
            ["SELL stop-loss must be above entry"],
        )

    if side not in ("BUY", "SELL"):
        return ValidationResult(
            False,
            ["Side must be BUY or SELL"],
        )

    return ValidationResult(True, [])


def validate_take_profit(
    side: str,
    entry: float,
    take_profit: float,
) -> ValidationResult:
    """Check take-profit placement."""

    if entry <= 0 or take_profit <= 0:
        return ValidationResult(False, ["Prices must be positive"])

    if side == "BUY" and take_profit <= entry:
        return ValidationResult(
            False,
            ["BUY take-profit must be above entry"],
        )

    if side == "SELL" and take_profit >= entry:
        return ValidationResult(
            False,
            ["SELL take-profit must be below entry"],
        )

    if side not in ("BUY", "SELL"):
        return ValidationResult(
            False,
            ["Side must be BUY or SELL"],
        )

    return ValidationResult(True, [])


def calculate_risk_reward(
    side: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
) -> Optional[float]:
    """Calculate reward-to-risk ratio."""

    if side == "BUY":
        risk = entry - stop_loss
        reward = take_profit - entry

    elif side == "SELL":
        risk = stop_loss - entry
        reward = entry - take_profit

    else:
        return None

    if risk <= 0 or reward <= 0:
        return None

    return reward / risk


def validate_risk_reward(
    side: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    minimum_rr: float = 1.5,
) -> ValidationResult:
    """Reject trades with insufficient reward-to-risk."""

    rr = calculate_risk_reward(
        side,
        entry,
        stop_loss,
        take_profit,
    )

    if rr is None:
        return ValidationResult(
            False,
            ["Unable to calculate valid risk/reward"],
        )

    if rr < minimum_rr:
        return ValidationResult(
            False,
            [
                f"Risk/reward too low: {rr:.2f}; "
                f"minimum required: {minimum_rr:.2f}"
            ],
        )

    return ValidationResult(
        True,
        [f"Risk/reward accepted: {rr:.2f}"],
    )


def validate_atr_distance(
    side: str,
    entry: float,
    stop_loss: float,
    atr_value: float,
    minimum_atr: float = 0.5,
    maximum_atr: float = 3.0,
) -> ValidationResult:
    """Check stop-loss distance relative to ATR."""

    if atr_value <= 0:
        return ValidationResult(
            False,
            ["ATR must be positive"],
        )

    if side not in ("BUY", "SELL"):
        return ValidationResult(
            False,
            ["Side must be BUY or SELL"],
        )

    distance = abs(entry - stop_loss)
    atr_multiple = distance / atr_value

    if atr_multiple < minimum_atr:
        return ValidationResult(
            False,
            [f"Stop-loss too tight: {atr_multiple:.2f} ATR"],
        )

    if atr_multiple > maximum_atr:
        return ValidationResult(
            False,
            [f"Stop-loss too wide: {atr_multiple:.2f} ATR"],
        )

    return ValidationResult(
        True,
        [f"Stop-loss distance accepted: {atr_multiple:.2f} ATR"],
    )


def validate_trade(
    side: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    atr_value: float,
    minimum_rr: float = 1.5,
) -> ValidationResult:
    """Run all trade-level validation checks."""

    results = [
        validate_stop_loss(side, entry, stop_loss),
        validate_take_profit(side, entry, take_profit),
        validate_risk_reward(
            side,
            entry,
            stop_loss,
            take_profit,
            minimum_rr,
        ),
        validate_atr_distance(
            side,
            entry,
            stop_loss,
            atr_value,
        ),
    ]

    reasons = []

    for result in results:
        reasons.extend(result.reasons)

    valid = all(result.valid for result in results)

    return ValidationResult(
        valid=valid,
        reasons=reasons,
  )
      
