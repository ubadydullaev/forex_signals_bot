
import logging
from datetime import datetime

TIMEFRAME_MS = {
    "M15": 15 * 60 * 1000,
    "M30": 30 * 60 * 1000,
    "H1": 60 * 60 * 1000,
    "H4": 4 * 60 * 60 * 1000,
}

MAX_TRACKED_SIGNALS = 300


def signal_timestamp_ms(value):
    dt = datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    if dt.tzinfo is None:
        raise ValueError("Timezone yo'q")
    return int(dt.timestamp() * 1000)


def register_tracked_signal(state, signal, record):
    delivered = [
        str(x) for x in record.get("delivered_to", [])
    ]

    if not delivered:
        return False

    tf = signal["timeframe"]

    if tf not in TIMEFRAME_MS:
        return False

    key = (
        f"{signal['symbol']}_{tf}_"
        f"{signal['candle_time']}_{signal['side']}"
    )

    tracked = state.setdefault("tracked_signals", {})

    if key in tracked:
        return False

    tracked[key] = {
        "symbol": signal["symbol"],
        "timeframe": tf,
        "side": signal["side"],
        "entry": float(signal["entry"]),
        "sl": float(signal["sl"]),
        "tp1": float(signal["tp1"]),
        "signal_candle_time": signal["candle_time"],
        "signal_candle_end_ms": (
            signal_timestamp_ms(signal["candle_time"])
            + TIMEFRAME_MS[tf]
        ),
        "delivered_to": delivered,
        "notified_to": [],
        "status": "OPEN",
        "diagnostics": {
            name: signal[name]
            for name in (
                "score", "adx", "plus_di", "minus_di",
                "rsi", "atr", "atr_risk_ratio",
                "ema20", "ema50", "ema200",
                "supertrend", "h1_direction",
                "h4_direction", "structure_buy",
                "structure_sell"
            )
            if name in signal
        },
        "created_at": datetime.now().astimezone().isoformat(),
    }

    logging.info("Tracker: %s ro'yxatga olindi", key)

    if len(tracked) > MAX_TRACKED_SIGNALS:
        finished = [
            (k, v) for k, v in tracked.items()
            if v.get("status") != "OPEN"
        ]

        finished.sort(
            key=lambda x: x[1].get("created_at", "")
        )

        while len(tracked) > MAX_TRACKED_SIGNALS and finished:
            old_key, _ = finished.pop(0)
            tracked.pop(old_key, None)

    return True


def format_result(item):
    symbol = item["symbol"]

    digits = (
        1 if symbol in ("US500", "DE30")
else 2 if symbol in ("XAUUSD", "BRENT", "BTCUSD")
else 3 if symbol == "XAGUSD" or symbol.endswith("JPY")
else 5
    )

    status = item["status"]

    if status == "TP1_HIT":
        heading = "🎯 <b>TP1 HIT</b>"
        detail = "Birinchi take-profit darajasiga yetdi."
    elif status == "SL_HIT":
        heading = "🛑 <b>SL HIT</b>"
        detail = "Stop-loss darajasiga yetdi."
    else:
        heading = "⚠️ <b>AMBIGUOUS</b>"
        detail = (
            "Bitta shamda TP1 va SL ikkalasiga ham tegdi. "
            "Qaysi biri avval bo'lganini aniqlab bo'lmadi."
        )

    return "\n".join([
        heading,
        f"📊 {symbol} — {item['side']} | {item['timeframe']}",
        f"Entry: <code>{item['entry']:.{digits}f}</code>",
        f"SL: <code>{item['sl']:.{digits}f}</code>",
        f"TP1: <code>{item['tp1']:.{digits}f}</code>",
        detail,
        f"Signal: {item['signal_candle_time']}",
        "ℹ️ Keyingi yopilgan shamlar asosida tekshirildi.",
    ])


def evaluate_tracked_signals(state, candle_cache, send_telegram):
    tracked = state.setdefault("tracked_signals", {})
    changed = False

    for key, item in list(tracked.items()):
        candles = candle_cache.get(
            (item["symbol"], item["timeframe"]), []
        )

        if item.get("status") == "OPEN" and candles:
            side = item["side"]
            tp = float(item["tp1"])
            sl = float(item["sl"])
            start = int(item["signal_candle_end_ms"])

            for candle in candles:
                if int(candle["timestamp"]) < start:
                    continue

                if side == "BUY":
                    tp_hit = candle["high"] >= tp
                    sl_hit = candle["low"] <= sl
                else:
                    tp_hit = candle["low"] <= tp
                    sl_hit = candle["high"] >= sl

                if tp_hit and sl_hit:
                    item["status"] = "AMBIGUOUS"
                elif tp_hit:
                    item["status"] = "TP1_HIT"
                elif sl_hit:
                    item["status"] = "SL_HIT"
                else:
                    continue

                item["result_candle_time"] = candle["time"]
                changed = True

                logging.info(
                    "Tracker natija: %s -> %s",
                    key, item["status"]
                )
                break

        if item.get("status") in (
            "TP1_HIT", "SL_HIT", "AMBIGUOUS"
        ):
            notified = set(item.get("notified_to", []))

            for user_id in item.get("delivered_to", []):
                user_id = str(user_id)

                if user_id in notified:
                    continue

                try:
                    send_telegram(
                        user_id,
                        format_result(item)
                    )
                    notified.add(user_id)
                    item["notified_to"] = sorted(notified)
                    changed = True
                except Exception:
                    logging.exception(
                        "Tracker xabari yuborilmadi: %s",
                        user_id
                    )

    return changed
  
