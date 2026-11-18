"""
Advanced confluence analysis module for Forex Signals Bot.

This module is intentionally independent from the Telegram/broker layer.
It reads CLOSED OHLCV candles and returns a structured analysis dictionary.
It does not place orders and does not replace the bot's existing signal engine.

Dependencies:
    pandas
    numpy

Expected DataFrame columns (case-insensitive):
    open, high, low, close
Optional:
    volume

Example:
    from advanced_analysis import analyze_advanced

    result = analyze_advanced(
        entry_df=m15_df,
        higher_timeframes={"H1": h1_df, "H4": h4_df},
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class AdvancedConfig:
    ema_fast: int = 21
    ema_mid: int = 50
    ema_slow: int = 200
    atr_period: int = 14
    rsi_period: int = 14
    adx_period: int = 14
    pivot_left: int = 3
    pivot_right: int = 3
    structure_lookback: int = 180
    fvg_lookback: int = 80
    volume_period: int = 20
    kalman_period: int = 20
    min_score: int = 65
    min_direction_margin: int = 12
    min_rr: float = 1.5
    target_rr: float = 2.0
    stop_atr_buffer: float = 0.20
    max_bars: int = 1200


DEFAULT_CONFIG = AdvancedConfig()


def _normalise_frame(frame: pd.DataFrame, max_bars: int) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("Candles must be supplied as a pandas DataFrame.")

    aliases = {str(c).strip().lower(): c for c in frame.columns}
    required = ("open", "high", "low", "close")
    missing = [c for c in required if c not in aliases]
    if missing:
        raise ValueError("Missing OHLC columns: " + ", ".join(missing))

    out = pd.DataFrame(index=frame.index)
    for name in required:
        out[name] = pd.to_numeric(frame[aliases[name]], errors="coerce")

    if "volume" in aliases:
        out["volume"] = pd.to_numeric(frame[aliases["volume"]], errors="coerce")
    else:
        out["volume"] = np.nan

    out = out.replace([np.inf, -np.inf], np.nan)
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out.tail(max_bars).copy()

    if len(out) < 60:
        raise ValueError(f"At least 60 valid candles are required; received {len(out)}.")

    # Reject malformed candles rather than silently creating false levels.
    valid = (
        (out["high"] >= out[["open", "close", "low"]].max(axis=1))
        & (out["low"] <= out[["open", "close", "high"]].min(axis=1))
    )
    out = out.loc[valid].copy()
    if len(out) < 60:
        raise ValueError("Fewer than 60 valid OHLC candles remain after validation.")
    return out


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=max(2, period // 3)).mean()


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    result = result.where(loss.notna(), 50.0)
    result = result.mask((loss == 0) & (gain > 0), 100.0)
    result = result.mask((gain == 0) & (loss > 0), 0.0)
    return result.fillna(50.0)


def _adx(df: pd.DataFrame, period: int) -> Tuple[pd.Series, pd.Series, pd.Series]:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=df.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=df.index,
    )

    atr = _atr(df, period).replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx.fillna(0.0), plus_di.fillna(0.0), minus_di.fillna(0.0)


def _kalman_filter(close: pd.Series, period: int) -> pd.Series:
    """One-state robust Kalman estimate; large shocks receive less weight."""
    values = close.to_numpy(dtype=float)
    result = np.empty(len(values), dtype=float)
    estimate = values[0]
    variance = 1.0
    noise_window = max(8, period)
    residuals = []

    for i, price in enumerate(values):
        if i == 0:
            result[i] = estimate
            continue

        predicted_variance = variance + 1.0 / (period * period)
        surprise = price - estimate
        recent = residuals[-noise_window:]
        baseline = float(np.median(np.square(recent))) if recent else max(
            float(np.var(values[: min(i + 1, noise_window)])), 1e-8
        )
        baseline = max(baseline, 1e-8)
        anomaly = (surprise * surprise) / baseline

        measurement_noise = baseline
        if anomaly > 9.0:
            measurement_noise *= min(25.0, 1.0 + anomaly / 9.0)

        gain = predicted_variance / (predicted_variance + measurement_noise)
        estimate = estimate + gain * surprise
        variance = max((1.0 - gain) * predicted_variance, 1e-10)
        residuals.append(surprise)
        result[i] = estimate

    return pd.Series(result, index=close.index)


def _last_confirmed_pivot(
    df: pd.DataFrame, side: str, left: int, right: int, lookback: int
) -> Optional[Tuple[int, float]]:
    """Return (position, price) for the latest pivot confirmed by available bars."""
    col = "high" if side == "high" else "low"
    values = df[col].to_numpy(dtype=float)
    n = len(values)
    start = max(left, n - lookback - right - 1)
    end = n - right
    found = None

    for i in range(start, end):
        window = values[i - left : i + right + 1]
        if len(window) != left + right + 1:
            continue
        candidate = values[i]
        if side == "high":
            is_pivot = candidate >= np.max(window[:left]) and candidate > np.max(window[left + 1 :])
        else:
            is_pivot = candidate <= np.min(window[:left]) and candidate < np.min(window[left + 1 :])
        if is_pivot:
            found = (i, float(candidate))
    return found


def _latest_fvg(df: pd.DataFrame, lookback: int) -> Optional[Dict[str, Any]]:
    """Find the latest still-valid three-candle imbalance."""
    start = max(2, len(df) - lookback)
    found = None

    for i in range(start, len(df)):
        # Bullish gap: current low is above the high two candles earlier.
        bull_low = float(df["high"].iloc[i - 2])
        bull_high = float(df["low"].iloc[i])
        if bull_high > bull_low:
            later = df.iloc[i + 1 :]
            invalidated = not later.empty and bool((later["close"] < bull_low).any())
            if not invalidated:
                found = {
                    "direction": "BUY",
                    "low": bull_low,
                    "high": bull_high,
                    "bar_position": i,
                }

        # Bearish gap: current high is below the low two candles earlier.
        bear_low = float(df["high"].iloc[i])
        bear_high = float(df["low"].iloc[i - 2])
        if bear_high > bear_low:
            later = df.iloc[i + 1 :]
            invalidated = not later.empty and bool((later["close"] > bear_high).any())
            if not invalidated:
                found = {
                    "direction": "SELL",
                    "low": bear_low,
                    "high": bear_high,
                    "bar_position": i,
                }

    return found


def _timeframe_bias(frame: pd.DataFrame, cfg: AdvancedConfig) -> Dict[str, Any]:
    data = _normalise_frame(frame, cfg.max_bars)
    close = data["close"]
    fast = _ema(close, cfg.ema_fast)
    mid = _ema(close, cfg.ema_mid)
    slow = _ema(close, cfg.ema_slow)
    atr = _atr(data, cfg.atr_period)
    kalman = _kalman_filter(close, cfg.kalman_period)

    last = float(close.iloc[-1])
    mid_now = float(mid.iloc[-1]) if pd.notna(mid.iloc[-1]) else last
    slow_now = float(slow.iloc[-1]) if pd.notna(slow.iloc[-1]) else last
    mid_prev = float(mid.iloc[-4]) if len(mid) >= 4 and pd.notna(mid.iloc[-4]) else mid_now
    k_now = float(kalman.iloc[-1])
    k_prev = float(kalman.iloc[-4]) if len(kalman) >= 4 else k_now

    if last > mid_now and mid_now > slow_now and mid_now >= mid_prev and k_now >= k_prev:
        direction = 1
    elif last < mid_now and mid_now < slow_now and mid_now <= mid_prev and k_now <= k_prev:
        direction = -1
    else:
        direction = 0

    return {
        "direction": direction,
        "close": last,
        "ema_mid": mid_now,
        "ema_slow": slow_now,
        "atr": float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else None,
    }


def analyze_advanced(
    entry_df: pd.DataFrame,
    higher_timeframes: Optional[Mapping[str, pd.DataFrame]] = None,
    config: AdvancedConfig = DEFAULT_CONFIG,
) -> Dict[str, Any]:
    """
    Analyze the latest CLOSED entry-timeframe candle.

    higher_timeframes example:
        {"H1": h1_df, "H4": h4_df}

    Returns a JSON-serialisable dictionary. If there is not enough evidence,
    signal is HOLD. This module is advisory only.
    """
    df = _normalise_frame(entry_df, config.max_bars)
    close, high, low = df["close"], df["high"], df["low"]

    ema_fast = _ema(close, config.ema_fast)
    ema_mid = _ema(close, config.ema_mid)
    ema_slow = _ema(close, config.ema_slow)
    atr_s = _atr(df, config.atr_period)
    rsi_s = _rsi(close, config.rsi_period)
    adx_s, plus_di_s, minus_di_s = _adx(df, config.adx_period)
    kalman = _kalman_filter(close, config.kalman_period)

    last = float(close.iloc[-1])
    previous_close = float(close.iloc[-2])
    last_high = float(high.iloc[-1])
    last_low = float(low.iloc[-1])
    atr = float(atr_s.iloc[-1]) if pd.notna(atr_s.iloc[-1]) else 0.0
    rsi = float(rsi_s.iloc[-1])
    adx = float(adx_s.iloc[-1])
    plus_di = float(plus_di_s.iloc[-1])
    minus_di = float(minus_di_s.iloc[-1])
    e_fast = float(ema_fast.iloc[-1]) if pd.notna(ema_fast.iloc[-1]) else last
    e_mid = float(ema_mid.iloc[-1]) if pd.notna(ema_mid.iloc[-1]) else last
    e_slow = float(ema_slow.iloc[-1]) if pd.notna(ema_slow.iloc[-1]) else last
    k_now = float(kalman.iloc[-1])
    k_prev = float(kalman.iloc[-4]) if len(kalman) >= 4 else k_now

    local_trend = 1 if last > e_mid and e_mid > e_slow else (
        -1 if last < e_mid and e_mid < e_slow else 0
    )

    htf_results: Dict[str, Dict[str, Any]] = {}
    for name, frame in (higher_timeframes or {}).items():
        try:
            htf_results[str(name)] = _timeframe_bias(frame, config)
        except (TypeError, ValueError, KeyError):
            # One bad optional timeframe must not crash the existing bot.
            htf_results[str(name)] = {"direction": 0, "error": "insufficient_or_invalid_data"}

    valid_htf = [v["direction"] for v in htf_results.values() if "error" not in v]
    htf_bias = 0
    if valid_htf:
        bullish = sum(x == 1 for x in valid_htf)
        bearish = sum(x == -1 for x in valid_htf)
        if bullish == len(valid_htf):
            htf_bias = 1
        elif bearish == len(valid_htf):
            htf_bias = -1

    swing_high = _last_confirmed_pivot(
        df, "high", config.pivot_left, config.pivot_right, config.structure_lookback
    )
    swing_low = _last_confirmed_pivot(
        df, "low", config.pivot_left, config.pivot_right, config.structure_lookback
    )

    swing_high_price = swing_high[1] if swing_high else None
    swing_low_price = swing_low[1] if swing_low else None

    bull_bos = bool(
        swing_high_price is not None
        and previous_close <= swing_high_price
        and last > swing_high_price
    )
    bear_bos = bool(
        swing_low_price is not None
        and previous_close >= swing_low_price
        and last < swing_low_price
    )
    bull_sweep = bool(
        swing_low_price is not None
        and last_low < swing_low_price
        and last > swing_low_price
    )
    bear_sweep = bool(
        swing_high_price is not None
        and last_high > swing_high_price
        and last < swing_high_price
    )

    fvg = _latest_fvg(df, config.fvg_lookback)
    fvg_near = False
    fvg_direction = None
    if fvg and atr > 0:
        distance = max(fvg["low"] - last, 0.0, last - fvg["high"])
        fvg_near = distance <= atr * 0.50
        if fvg_near:
            fvg_direction = fvg["direction"]

    volume = df["volume"]
    volume_avg = volume.rolling(config.volume_period, min_periods=5).mean()
    vol_now = float(volume.iloc[-1]) if pd.notna(volume.iloc[-1]) else None
    vol_mean = float(volume_avg.iloc[-1]) if pd.notna(volume_avg.iloc[-1]) else None
    relative_volume = (
        vol_now / vol_mean if vol_now is not None and vol_mean and vol_mean > 0 else None
    )

    candle_range = max(last_high - last_low, 1e-12)
    body_ratio = abs(last - float(df["open"].iloc[-1])) / candle_range
    bullish_candle = last > float(df["open"].iloc[-1])
    bearish_candle = last < float(df["open"].iloc[-1])

    # Distinct evidence groups avoid counting several correlated EMAs as separate votes.
    buy_score = 0
    sell_score = 0
    reasons_buy = []
    reasons_sell = []

    # 1) Higher-timeframe and local trend: 25 points.
    if htf_bias == 1:
        buy_score += 18
        reasons_buy.append("Higher timeframes bullish")
    elif htf_bias == -1:
        sell_score += 18
        reasons_sell.append("Higher timeframes bearish")

    if local_trend == 1:
        buy_score += 7
        reasons_buy.append("Local EMA trend bullish")
    elif local_trend == -1:
        sell_score += 7
        reasons_sell.append("Local EMA trend bearish")

    # 2) Structure/liquidity: 25 points.
    if bull_bos:
        buy_score += 20
        reasons_buy.append("Confirmed bullish BOS")
    if bear_bos:
        sell_score += 20
        reasons_sell.append("Confirmed bearish BOS")
    if bull_sweep:
        buy_score += 15
        reasons_buy.append("Bullish liquidity sweep")
    if bear_sweep:
        sell_score += 15
        reasons_sell.append("Bearish liquidity sweep")

    # 3) Location: 15 points.
    if fvg_near and fvg_direction == "BUY":
        buy_score += 15
        reasons_buy.append("Price near valid bullish FVG")
    elif fvg_near and fvg_direction == "SELL":
        sell_score += 15
        reasons_sell.append("Price near valid bearish FVG")
    elif swing_low_price is not None and atr > 0 and abs(last - swing_low_price) <= atr * 0.35:
        buy_score += 8
        reasons_buy.append("Near confirmed swing-low support")
    elif swing_high_price is not None and atr > 0 and abs(last - swing_high_price) <= atr * 0.35:
        sell_score += 8
        reasons_sell.append("Near confirmed swing-high resistance")

    # 4) Momentum/strength: 20 points.
    if rsi >= 52 and plus_di > minus_di and k_now > k_prev:
        buy_score += 12
        reasons_buy.append("Bullish momentum and Kalman slope")
    elif rsi <= 48 and minus_di > plus_di and k_now < k_prev:
        sell_score += 12
        reasons_sell.append("Bearish momentum and Kalman slope")

    if adx >= 20:
        if plus_di > minus_di:
            buy_score += 8
            reasons_buy.append("ADX confirms directional strength")
        elif minus_di > plus_di:
            sell_score += 8
            reasons_sell.append("ADX confirms directional strength")

    # 5) Candle pressure and relative volume: 10 points.
    if body_ratio >= 0.55:
        if bullish_candle:
            buy_score += 5
            reasons_buy.append("Strong bullish candle body")
        elif bearish_candle:
            sell_score += 5
            reasons_sell.append("Strong bearish candle body")

    if relative_volume is not None and relative_volume >= 1.20:
        if bullish_candle:
            buy_score += 5
            reasons_buy.append("Relative volume expansion")
        elif bearish_candle:
            sell_score += 5
            reasons_sell.append("Relative volume expansion")

    # 6) Breakout / compression release: 5 points.
    prior_high = float(high.iloc[-21:-1].max()) if len(df) >= 21 else None
    prior_low = float(low.iloc[-21:-1].min()) if len(df) >= 21 else None
    if prior_high is not None and last > prior_high and bullish_candle:
        buy_score += 5
        reasons_buy.append("20-bar range breakout")
    if prior_low is not None and last < prior_low and bearish_candle:
        sell_score += 5
        reasons_sell.append("20-bar range breakdown")

    buy_score = min(100, buy_score)
    sell_score = min(100, sell_score)
    margin = abs(buy_score - sell_score)

    signal = "HOLD"
    direction = 0
    if buy_score >= config.min_score and buy_score > sell_score and margin >= config.min_direction_margin:
        signal, direction = "BUY", 1
    elif sell_score >= config.min_score and sell_score > buy_score and margin >= config.min_direction_margin:
        signal, direction = "SELL", -1

    entry = last
    stop = None
    target = None
    rr = None
    risk = None
    if direction != 0 and atr > 0:
        if direction == 1:
            structural_stop = (
                swing_low_price - atr * config.stop_atr_buffer
                if swing_low_price is not None
                else last - atr * 1.5
            )
            stop = min(structural_stop, last - atr * 0.8)
            risk = entry - stop
            target = entry + risk * config.target_rr
        else:
            structural_stop = (
                swing_high_price + atr * config.stop_atr_buffer
                if swing_high_price is not None
                else last + atr * 1.5
            )
            stop = max(structural_stop, last + atr * 0.8)
            risk = stop - entry
            target = entry - risk * config.target_rr

        if risk and risk > 0:
            rr = abs(target - entry) / risk
        if rr is None or rr < config.min_rr:
            signal, direction = "HOLD", 0
            entry = stop = target = rr = risk = None

    def clean_number(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            number = float(value)
            return number if np.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    return {
        "signal": signal,
        "direction": direction,
        "buy_score": int(buy_score),
        "sell_score": int(sell_score),
        "score_margin": int(margin),
        "entry": clean_number(entry),
        "stop_loss": clean_number(stop),
        "take_profit": clean_number(target),
        "risk_reward": clean_number(rr),
        "market": {
            "close": clean_number(last),
            "atr": clean_number(atr),
            "rsi": clean_number(rsi),
            "adx": clean_number(adx),
            "plus_di": clean_number(plus_di),
            "minus_di": clean_number(minus_di),
            "ema_fast": clean_number(e_fast),
            "ema_mid": clean_number(e_mid),
            "ema_slow": clean_number(e_slow),
            "kalman_estimate": clean_number(k_now),
            "relative_volume": clean_number(relative_volume),
            "candle_body_ratio": clean_number(body_ratio),
        },
        "structure": {
            "trend": "BULLISH" if local_trend == 1 else "BEARISH" if local_trend == -1 else "NEUTRAL",
            "higher_timeframe_bias": "BULLISH" if htf_bias == 1 else "BEARISH" if htf_bias == -1 else "MIXED_OR_NEUTRAL",
            "swing_high": clean_number(swing_high_price),
            "swing_low": clean_number(swing_low_price),
            "bullish_bos": bull_bos,
            "bearish_bos": bear_bos,
            "bullish_sweep": bull_sweep,
            "bearish_sweep": bear_sweep,
            "fvg": (
                {
                    "direction": fvg["direction"],
                    "low": clean_number(fvg["low"]),
                    "high": clean_number(fvg["high"]),
                    "near_price": fvg_near,
                }
                if fvg
                else None
            ),
        },
        "higher_timeframes": htf_results,
        "reasons": {
            "buy": reasons_buy,
            "sell": reasons_sell,
        },
        "meta": {
            "module": "advanced_confluence_v1",
            "closed_candle_only": True,
            "volume_note": "Forex volume is commonly tick volume; treat relative-volume evidence as a heuristic.",
            "execution": "No orders are placed. Entry is the last supplied candle close; live integration must use closed candles.",
        },
    }
