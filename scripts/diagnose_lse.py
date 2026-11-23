
import os
import csv
import io
import time
import requests

from datetime import datetime, timedelta, timezone

API_URL = "https://download.londonstrategicedge.com/v3"
API_KEY = os.getenv("LSE_API_KEY", "").strip()

MIN_CANDLES = 220

INSTRUMENTS = {
    "EURUSD": ("EUR/USD", "forex"),
    "GBPUSD": ("GBP/USD", "forex"),
    "XAUUSD": ("XAU/USD", "commodities"),
    "XAGUSD": ("XAG/USD", "commodities"),
    "US500": ("US500", "indices"),
}

TIMEFRAMES = {
    "M15": ("15m", 900),
    "M30": ("30m", 1800),
    "H1": ("1h", 3600),
    "H4": ("4h", 14400),
    "D1": ("1d", 86400),
}

session = requests.Session()
session.headers.update({
    "X-API-Key": API_KEY,
    "User-Agent": "ForexSignalsBot-LSE-Diagnostic/1.0",
})


def log(message):
    print(message, flush=True)


def normalize_symbol(value):
    return "".join(ch for ch in str(value).upper() if ch.isalnum())


def api_get(path, params=None):
    time.sleep(1.05)

    response = session.get(
        f"{API_URL}{path}",
        params=params or {},
        timeout=60,
    )

    if response.status_code != 200:
        try:
            body = response.json()
        except Exception:
            body = response.text[:300]

        raise RuntimeError(
            f"HTTP {response.status_code}: {body}"
        )

    return response


def find_instrument(bot_symbol, search_query, expected_market):
    log(f"[CATALOGUE] {bot_symbol} q={search_query}")

    response = api_get(
        "/pairs",
        {
            "q": search_query,
            "limit": 100,
        },
    )

    data = response.json()
    pairs = data.get("pairs", [])

    target = normalize_symbol(bot_symbol)

    matches = [
        pair for pair in pairs
        if normalize_symbol(pair.get("symbol", "")) == target
    ]

    if not matches:
        log(
            f"[NOT FOUND] {bot_symbol} | "
            f"q={search_query} | results={len(pairs)}"
        )

        for pair in pairs[:8]:
            log(
                f"  candidate: {pair.get('symbol')} | "
                f"{pair.get('name')} | {pair.get('folder')}"
            )

        return None

    matches.sort(
        key=lambda pair: (
            pair.get("folder") != expected_market,
            not pair.get("active", True),
        )
    )

    selected = matches[0]

    log(
        f"[FOUND] {bot_symbol} -> "
        f"{selected.get('symbol')} | "
        f"{selected.get('name')} | "
        f"market={selected.get('folder')} | "
        f"active={selected.get('active')}"
    )

    return selected


def parse_time(value):
    value = str(value).strip()

    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except Exception:
        return None


def inspect_candles(pair, bot_symbol, timeframe, interval):
    now = datetime.now(timezone.utc)

    if timeframe == "D1":
        start_date = (now - timedelta(days=500)).date()
    else:
        start_date = (now - timedelta(days=180)).date()

    end_date = now.date()

    params = {
        "market": pair["folder"],
        "symbol": pair["symbol"],
        "timeframe": TIMEFRAMES[timeframe][0],
        "from": start_date.isoformat(),
        "to": end_date.isoformat(),
        "format": "csv",
        "data": "candles",
    }

    log(
        f"[REQUEST] {bot_symbol} {timeframe} | "
        f"{start_date} -> {end_date}"
    )

    response = api_get("/export", params)

    content = response.content.decode(
        "utf-8-sig",
        errors="replace",
    )

    if content.lstrip().startswith("{"):
        try:
            error = response.json()
        except Exception:
            error = content[:300]

        raise RuntimeError(f"API returned JSON: {error}")

    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)

    if not rows:
        log(f"[EMPTY] {bot_symbol} {timeframe}")
        return 0

    closed_times = set()
    invalid = 0

    for row in rows:
        timestamp_value = (
            row.get("time")
            or row.get("timestamp")
            or row.get("datetime")
            or row.get("date")
        )

        candle_time = parse_time(timestamp_value)

        if candle_time is None:
            invalid += 1
            continue

        try:
            open_price = float(row["open"])
            high_price = float(row["high"])
            low_price = float(row["low"])
            close_price = float(row["close"])

            if (
                min(open_price, high_price, low_price, close_price) <= 0
                or high_price < max(open_price, close_price, low_price)
                or low_price > min(open_price, close_price, high_price)
            ):
                invalid += 1
                continue

        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue

        if timeframe == "D1":
            # Bugungi kun shamini yopilgan deb hisoblamaymiz.
            if candle_time.date() >= now.date():
                continue
        else:
            # Faqat to'liq yopilgan shamlarni sanaymiz.
            if candle_time + timedelta(seconds=interval) > now:
                continue

        closed_times.add(candle_time)

    count = len(closed_times)
    status = "PASS" if count >= MIN_CANDLES else "FAIL"

    log(
        f"[{status}] {bot_symbol} {timeframe} | "
        f"closed={count} | raw={len(rows)} | "
        f"invalid={invalid} | required={MIN_CANDLES}"
    )

    return count


def main():
    if not API_KEY:
        raise SystemExit(
            "ERROR: GitHub Secret LSE_API_KEY topilmadi."
        )

    log("=" * 65)
    log("LONDON STRATEGIC EDGE - V6 DATA DIAGNOSTIC")
    log(f"UTC: {datetime.now(timezone.utc).isoformat()}")
    log(f"Minimum closed candles: {MIN_CANDLES}")
    log("=" * 65)

    try:
        usage = api_get("/usage").json()

        log(
            f"[AUTH OK] plan={usage.get('plan')} | "
            f"calls/min={usage.get('calls_per_minute')} | "
            f"markets={usage.get('markets')}"
        )

        if usage.get("withheld"):
            log(f"[WARNING] Key withheld: {usage['withheld']}")

    except Exception as exc:
        raise SystemExit(f"AUTH ERROR: {exc}")

    results = {}
    found_pairs = {}

    for symbol, (query, expected_market) in INSTRUMENTS.items():
        try:
            pair = find_instrument(
                symbol,
                query,
                expected_market,
            )
            found_pairs[symbol] = pair

        except Exception as exc:
            log(f"[CATALOGUE ERROR] {symbol}: {exc}")
            found_pairs[symbol] = None

    log("=" * 65)
    log("CANDLE TEST START")
    log("=" * 65)

    for symbol in INSTRUMENTS:
        pair = found_pairs.get(symbol)

        for timeframe, (_, interval) in TIMEFRAMES.items():
            key = f"{symbol}_{timeframe}"

            if pair is None:
                results[key] = 0
                log(f"[SKIP] {key} | exact instrument not found")
                continue

            try:
                results[key] = inspect_candles(
                    pair,
                    symbol,
                    timeframe,
                    interval,
                )

            except Exception as exc:
                results[key] = 0
                log(f"[ERROR] {key} | {exc}")

    passed = sum(
        1 for count in results.values()
        if count >= MIN_CANDLES
    )

    failed = len(results) - passed

    log("=" * 65)
    log("FINAL RESULTS")
    log("=" * 65)

    for symbol in INSTRUMENTS:
        for timeframe in TIMEFRAMES:
            key = f"{symbol}_{timeframe}"
            count = results.get(key, 0)
            status = "PASS" if count >= MIN_CANDLES else "FAIL"

            log(
                f"{status:4} | {key:12} | "
                f"{count:6} closed candles"
            )

    log("-" * 65)
    log(f"TOTAL: {passed}/25 passed")
    log(f"FAILED: {failed}/25")

    if passed == 25:
        log("[RESULT] ALL REQUIREMENTS PASSED")
    else:
        log("[RESULT] NOT READY FOR V6 INTEGRATION")

    log("=" * 65)


if __name__ == "__main__":
    main()
  
