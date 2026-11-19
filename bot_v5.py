
"""
Forex Signals Bot V5.1 — improved signal filtering.

Keep bot_v4.py, advanced_analysis.py, btc_data.py and
signal_tracker.py beside this file.
"""

import math
import pandas as pd

import bot_v4 as core
from advanced_analysis import analyze_advanced, AdvancedConfig


# ---------------------------------------------------------
# INSTRUMENTS AND CONFIGURATION
# ---------------------------------------------------------

# Brent is disabled.
core.SYMBOLS = [s for s in core.SYMBOLS if s != "BRENT"]
core.DUKASCOPY_SYMBOLS.pop("BRENT", None)

# New risk/reward settings.
core.MIN_REWARD_RISK = 1.5
core.PREFERRED_REWARD_RISK = 1.5

CONFIG = AdvancedConfig(
    min_score=58,
    min_direction_margin=10,
    min_rr=1.5,
    target_rr=1.5,
)

MIN_CANDLES = 220
MIN_ADX = 15
MAX_ENTRY_EXTENSION_ATR = 2.0

_D1_CACHE = {}
_original_get_candles = core.get_candles


# ---------------------------------------------------------
# D1 CACHE
# ---------------------------------------------------------

def _get_candles_with_d1_cache(symbol, interval):
    data = _original_get_candles(symbol, interval)

    if interval == "1d":
        _D1_CACHE[symbol] = data

    return data


core.get_candles = _get_candles_with_d1_cache


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def _reject(symbol, timeframe, reason):
    core.logging.info(
        "[V5 REJECT] %s %s | %s",
        symbol, timeframe, reason
    )
    return None


def _higher_timeframes(timeframe, h1, h4, d1):
    frames = {}

    if timeframe in ("M15", "M30") and h1:
        frames["H1"] = pd.DataFrame(h1)

    if timeframe in ("M15", "M30", "H1") and h4:
        frames["H4"] = pd.DataFrame(h4)

    if timeframe in ("M15", "M30", "H1", "H4") and d1:
        frames["D1"] = pd.DataFrame(d1)

    return frames


def _safe_float(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------
# MAIN ANALYSIS
# ---------------------------------------------------------

def analyze_v5(symbol, timeframe, candles, h1, h4):
    """V5.1 signal analysis with improved filtering."""

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

    if min(lengths.values()) < MIN_CANDLES:
        return _reject(
            symbol,
            timeframe,
            f"Insufficient candles: {lengths}"
        )

    try:
        frames = _higher_timeframes(
            timeframe, h1, h4, d1
        )

        result = analyze_advanced(
            entry_df=pd.DataFrame(candles),
            higher_timeframes=frames,
            config=CONFIG,
        )

        buy_score = int(result.get("buy_score", 0))
        sell_score = int(result.get("sell_score", 0))
        margin = int(result.get("score_margin", 0))
        side = result.get("signal")

        htf = result.get("higher_timeframes", {})
        market = result.get("market", {})
        structure = result.get("structure", {})

        core.logging.info(
            "[V5 ANALYSIS] %s %s | Signal=%s | "
            "BUY=%s SELL=%s Margin=%s | HTF=%s",
            symbol,
            timeframe,
            side,
            buy_score,
            sell_score,
            margin,
            htf
        )

        if side not in ("BUY", "SELL"):
            return _reject(
                symbol,
                timeframe,
                f"HOLD | BUY={buy_score} SELL={sell_score} "
                f"Margin={margin} | Reasons={result.get('reasons', {})}"
            )

        entry = _safe_float(result.get("entry"))
        sl = _safe_float(result.get("stop_loss"))
        tp = _safe_float(result.get("take_profit"))
        rr = _safe_float(result.get("risk_reward"))

        if min(entry, sl, tp, rr) <= 0:
            return _reject(
                symbol, timeframe,
                "Invalid Entry/SL/TP/RR"
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

        if risk <= 0:
            return _reject(
                symbol, timeframe,
                "Risk is zero"
            )

        actual_rr = reward / risk

        if actual_rr < CONFIG.min_rr or rr < CONFIG.min_rr:
            return _reject(
                symbol,
                timeframe,
                f"RR below {CONFIG.min_rr:.1f} | "
                f"Actual={actual_rr:.2f} Calculated={rr:.2f}"
            )

        expected = 1 if side == "BUY" else -1

        # -------------------------------------------------
        # HIGHER TIMEFRAME FILTER
        # -------------------------------------------------
        # D1 opposite direction is a strong warning.
        # H4 opposite direction is also rejected on lower
        # entry timeframes.
        # Neutral HTF direction is allowed.
        d1_direction = htf.get("D1", {}).get("direction", 0)
        h4_direction = htf.get("H4", {}).get("direction", 0)

        if d1_direction == -expected:
            return _reject(
                symbol,
                timeframe,
                "D1 trend strongly opposes signal"
            )

        if timeframe in ("M15", "M30", "H1"):
            if h4_direction == -expected:
                return _reject(
                    symbol,
                    timeframe,
                    "H4 trend strongly opposes signal"
                )

        # -------------------------------------------------
        # MARKET QUALITY FILTER
        # -------------------------------------------------

        atr = _safe_float(market.get("atr"))
        adx = _safe_float(market.get("adx"))
        rsi = _safe_float(market.get("rsi"), 50)
        plus_di = _safe_float(market.get("plus_di"))
        minus_di = _safe_float(market.get("minus_di"))

        if atr <= 0:
            return _reject(
                symbol, timeframe,
                "ATR invalid"
            )

        # Avoid weak sideways markets unless there is
        # a confirmed structure break or liquidity sweep.
        bullish_structure = (
            structure.get("bullish_bos")
            or structure.get("bullish_sweep")
        )

        bearish_structure = (
            structure.get("bearish_bos")
            or structure.get("bearish_sweep")
        )

        has_structure = (
            bullish_structure if side == "BUY"
            else bearish_structure
        )

        if adx < MIN_ADX and not has_structure:
            return _reject(
                symbol,
                timeframe,
                f"Weak market strength: ADX={adx:.1f}"
            )

        # Avoid entries where price has already moved
        # too far away from its fast EMA.
        ema_fast = _safe_float(
            market.get("ema_fast"), entry
        )

        extension = abs(entry - ema_fast) / atr

        if extension > MAX_ENTRY_EXTENSION_ATR:
            return _reject(
                symbol,
                timeframe,
                f"Price overextended: {extension:.2f} ATR"
            )

        # Avoid buying extremely overbought conditions
        # or selling extremely oversold conditions.
        if side == "BUY" and rsi >= 78:
            return _reject(
                symbol, timeframe,
                f"BUY RSI too high: {rsi:.1f}"
            )

        if side == "SELL" and rsi <= 22:
            return _reject(
                symbol, timeframe,
                f"SELL RSI too low: {rsi:.1f}"
            )

        # Directional momentum should not strongly oppose
        # the proposed signal.
        if side == "BUY" and minus_di > plus_di * 1.35:
            return _reject(
                symbol, timeframe,
                "Bearish directional momentum conflict"
            )

        if side == "SELL" and plus_di > minus_di * 1.35:
            return _reject(
                symbol, timeframe,
                "Bullish directional momentum conflict"
            )

        # -------------------------------------------------
        # SIGNAL DATA
        # -------------------------------------------------

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
            "h1_direction": htf.get(
                "H1", {}
            ).get("direction", 0),
            "h4_direction": h4_direction,
            "structure_buy": buy_score,
            "structure_sell": sell_score,
        }

        core.logging.info(
            "[V5 ACCEPT] %s %s | %s | "
            "Score=%s | RR=%.2f | ADX=%.1f | RSI=%.1f",
            symbol,
            timeframe,
            side,
            score,
            actual_rr,
            adx,
            rsi
        )

        return signal

    except Exception:
        core.logging.exception(
            "[V5 ERROR] %s %s",
            symbol, timeframe
        )
        return None


# ---------------------------------------------------------
# CONNECT TO V4 ENGINE
# ---------------------------------------------------------

core.analyze = analyze_v5


if __name__ == "__main__":
    core.logging.info(
        "Forex Signals Bot V5.1 started | "
        "Brent OFF | RR 1:1.5 | "
        "D1/H4 trend filter | "
        "ADX/RSI/EMA extension filters ON"
    )

    core.main()
