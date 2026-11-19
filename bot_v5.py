"""
Forex Signals Bot V5 — conservative signal gate.

Place this file beside bot_v4.py and advanced_analysis.py.
The existing bot_v4.py remains the data/Telegram/state engine.
This wrapper replaces only the signal decision function at runtime.
"""

import math
import pandas as pd

import bot_v4 as core
from advanced_analysis import analyze_advanced, AdvancedConfig


# Remove Brent from every instrument lookup used by the old engine.
core.SYMBOLS = [s for s in core.SYMBOLS if s != "BRENT"]
core.DUKASCOPY_SYMBOLS.pop("BRENT", None)

# Enforce a minimum 1:2 risk/reward ratio in the legacy engine too.
core.MIN_REWARD_RISK = 2.0
core.PREFERRED_REWARD_RISK = 2.0

CONFIG = AdvancedConfig(
    min_score=65,
    min_direction_margin=12,
    min_rr=2.0,
    target_rr=2.0,
)

# V4 already downloads D1 candles in main(), but its original analyze()
# call passes only candles, H1 and H4. Capture the existing D1 download
# here so V5 can use it without changing bot_v4.py or downloading D1 again.
_D1_CACHE = {}
_original_get_candles = core.get_candles


def _get_candles_with_d1_cache(symbol, interval):
    data = _original_get_candles(symbol, interval)
    if interval == "1d":
        _D1_CACHE[symbol] = data
    return data


core.get_candles = _get_candles_with_d1_cache


def _higher_timeframes(timeframe, h1, h4, d1):
    frames = {}

    if timeframe in ("M15", "M30") and h1:
        frames["H1"] = pd.DataFrame(h1)

    if timeframe in ("M15", "M30", "H1") and h4:
        frames["H4"] = pd.DataFrame(h4)

    if timeframe in ("M15", "M30", "H1", "H4") and d1:
        frames["D1"] = pd.DataFrame(d1)

    return frames


def _reject(symbol, timeframe, reason):
    core.logging.info(
        "[V5 REJECT] %s %s | %s",
        symbol, timeframe, reason
    )
    return None


def analyze_v5(symbol, timeframe, candles, h1, h4):
    """V5 signal analysis with detailed rejection diagnostics."""

    d1 = _D1_CACHE.get(symbol)

    if not candles:
        return _reject(symbol, timeframe, "Entry candles missing")

    if not h1:
        return _reject(symbol, timeframe, "H1 candles missing")

    if not h4:
        return _reject(symbol, timeframe, "H4 candles missing")

    if not d1:
        return _reject(symbol, timeframe, "D1 candles missing")

    lengths = {
        "ENTRY": len(candles),
        "H1": len(h1),
        "H4": len(h4),
        "D1": len(d1),
    }

    if min(lengths.values()) < 220:
        return _reject(
            symbol, timeframe,
            f"Not enough candles: {lengths}"
        )

    try:
        result = analyze_advanced(
            entry_df=pd.DataFrame(candles),
            higher_timeframes=_higher_timeframes(
                timeframe, h1, h4, d1
            ),
            config=CONFIG,
        )

        buy_score = int(result.get("buy_score", 0))
        sell_score = int(result.get("sell_score", 0))
        margin = int(result.get("score_margin", 0))
        side = result.get("signal")

        htf = result.get("higher_timeframes", {})

        core.logging.info(
            "[V5 ANALYSIS] %s %s | Signal=%s | "
            "BUY=%s SELL=%s Margin=%s | HTF=%s",
            symbol, timeframe, side,
            buy_score, sell_score, margin, htf
        )

        if side not in ("BUY", "SELL"):
            reasons = result.get("reasons", {})
            return _reject(
                symbol, timeframe,
                f"HOLD | BUY={buy_score} SELL={sell_score} "
                f"Margin={margin} | Reasons={reasons}"
            )

        entry = float(result["entry"])
        sl = float(result["stop_loss"])
        tp = float(result["take_profit"])
        rr = float(result["risk_reward"])

        values = (entry, sl, tp, rr)

        if not all(math.isfinite(x) for x in values):
            return _reject(
                symbol, timeframe,
                "Invalid or non-finite Entry/SL/TP/RR"
            )

        if side == "BUY" and not (sl < entry < tp):
            return _reject(
                symbol, timeframe,
                "BUY price levels invalid"
            )

        if side == "SELL" and not (tp < entry < sl):
            return _reject(
                symbol, timeframe,
                "SELL price levels invalid"
            )

        risk = abs(entry - sl)
        reward = abs(tp - entry)
        actual_rr = reward / risk if risk > 0 else 0

        if risk <= 0:
            return _reject(
                symbol, timeframe, "Risk is zero or negative"
            )

        if actual_rr < 2.0 or rr < 2.0:
            return _reject(
                symbol, timeframe,
                f"RR below 1:2 | Actual={actual_rr:.2f} "
                f"Calculated={rr:.2f}"
            )

        required_by_tf = {
            "M15": ("H1", "H4", "D1"),
            "M30": ("H1", "H4", "D1"),
            "H1": ("H4", "D1"),
            "H4": ("D1",),
        }

        required = required_by_tf.get(
            timeframe, ("H1", "H4", "D1")
        )

        expected = 1 if side == "BUY" else -1

        for name in required:
            item = htf.get(name, {})
            direction = item.get("direction")

            if direction != expected:
                return _reject(
                    symbol, timeframe,
                    f"HTF mismatch: {name} direction={direction}, "
                    f"required={expected}"
                )

        market = result.get("market", {})

        atr = float(market.get("atr") or 0)
        rsi = float(market.get("rsi") or 50)
        adx = float(market.get("adx") or 0)
        plus_di = float(market.get("plus_di") or 0)
        minus_di = float(market.get("minus_di") or 0)

        if atr <= 0:
            return _reject(
                symbol, timeframe, "ATR is zero or invalid"
            )

        factor = core.usd_value_per_price_unit(
            symbol, entry
        )

        risk_usd = (
            risk * factor
            if factor is not None else None
        )

        reward_usd = (
            reward * factor
            if factor is not None else None
        )

        candle = candles[-1]

        score = max(buy_score, sell_score)

        signal = {
            "symbol": symbol,
            "timeframe": timeframe,
            "side": side,
            "score": score,
            "entry": entry,
            "sl": sl,
            "risk": risk,
            "tp1": tp,
            "tps": [tp],
            "risk_usd": risk_usd,
            "rewards_usd": [reward_usd],
            "reward_risks": [actual_rr],
            "candle_time": candle.get(
                "time", candle.get("timestamp")
            ),
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "rsi": rsi,
            "atr": atr,
            "atr_risk_ratio": risk / atr,
            "ema20": market.get("ema_fast") or entry,
            "ema50": market.get("ema_mid") or entry,
            "ema200": market.get("ema_slow") or entry,
            "supertrend": expected,
            "h1_direction": expected,
            "h4_direction": expected,
            "structure_buy": buy_score,
            "structure_sell": sell_score,
        }

        core.logging.info(
            "[V5 ACCEPT] %s %s | %s | Score=%s | RR=%.2f",
            symbol, timeframe, side, score, actual_rr
        )

        return signal

    except Exception:
        core.logging.exception(
            "[V5 ERROR] %s %s",
            symbol, timeframe
        )
        return None


# V4 main() resolves this global function at runtime.
core.analyze = analyze_v5


if __name__ == "__main__":
    core.logging.info(
        "Forex Signals Bot V5 started: Brent OFF, minimum RR 1:2, "
        "advanced H1/H4/D1 confirmation ON"
    )
    core.main()
