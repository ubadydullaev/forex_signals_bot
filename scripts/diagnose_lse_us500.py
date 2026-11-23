
import os
import io
import csv
import requests
from datetime import datetime, timedelta, timezone

BASE_URL = "https://download.londonstrategicedge.com/v3"
API_KEY = os.getenv("LSE_API_KEY")

SYMBOL = "SPSPX.IDX"
MARKET = "indices"

TIMEFRAMES = {
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
    "D1": "1d",
}

MIN_CANDLES = 220
HEADERS = {"X-API-Key": API_KEY} if API_KEY else {}


def request_api(path, params=None):
    response = requests.get(
        f"{BASE_URL}/{path}",
        headers=HEADERS,
        params=params,
        timeout=60,
    )
    print(f"[HTTP] {response.status_code} | {response.url}")
    response.raise_for_status()
    return response


def find_candidate():
    for query in ("SPSPX", "500", "S&P 500"):
        try:
            response = request_api("pairs", {"q": query})
            data = response.json()
            print(f"\n[CATALOG] Query={query}")
            print(str(data)[:12000])

            items = data if isinstance(data, list) else (
                data.get("pairs", data.get("data", []))
                if isinstance(data, dict) else []
            )

            for item in items:
                if not isinstance(item, dict):
                    continue

                symbol = str(
                    item.get("symbol")
                    or item.get("pair")
                    or item.get("code")
                    or ""
                ).strip()

                market = str(
                    item.get("market")
                    or item.get("folder")
                    or item.get("type")
                    or ""
                ).strip().lower()

                if symbol.upper() == SYMBOL and (
                    not market or market == MARKET
                ):
                    print("[CANDIDATE FOUND]", item)
                    return item

        except Exception as exc:
            print(f"[CATALOG ERROR] {query}: {exc}")

    print("[CANDIDATE NOT CONFIRMED IN CATALOG]")
    return None


def parse_rows(response):
    content_type = response.headers.get("Content-Type", "").lower()

    if "json" in content_type:
        data = response.json()
        if isinstance(data, dict):
            rows = data.get("candles", data.get("data", data.get("bars", [])))
        else:
            rows = data

        if not isinstance(rows, list):
            return []
        return rows

    text = response.text.strip()
    if not text:
        return []

    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def get_time(row):
    if not isinstance(row, dict):
        return None

    value = (
        row.get("timestamp")
        or row.get("time")
        or row.get("datetime")
        or row.get("date")
        or row.get("openTime")
    )

    if value is None:
        return None

    try:
        if isinstance(value, (int, float)):
            number = float(value)
            if number > 10_000_000_000:
                number /= 1000
            return datetime.fromtimestamp(number, tz=timezone.utc)

        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def timeframe_delta(timeframe):
    return {
        "15m": timedelta(minutes=15),
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
    }[timeframe]


def validate_rows(rows, timeframe):
    now = datetime.now(timezone.utc)
    delta = timeframe_delta(timeframe)
    valid = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        timestamp = get_time(row)
        if timestamp is None:
            continue

        try:
            o = float(row.get("open"))
            h = float(row.get("high"))
            low = float(row.get("low"))
            c = float(row.get("close"))

            if min(o, h, low, c) <= 0:
                continue
            if h < max(o, c, low) or low > min(o, c, h):
                continue
        except (TypeError, ValueError):
            continue

        if timestamp + delta > now:
            continue

        valid.append((timestamp, row))

    unique = {}
    for timestamp, row in valid:
        unique[timestamp] = row

    return sorted(unique.items(), key=lambda item: item[0])


def export_timeframe(label, timeframe):
    days = 500 if timeframe == "1d" else 180
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    params = {
        "market": MARKET,
        "symbol": SYMBOL,
        "timeframe": timeframe,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "format": "csv",
        "data": "candles",
    }

    try:
        response = request_api("export", params)
        rows = parse_rows(response)
        candles = validate_rows(rows, timeframe)

        count = len(candles)
        first = candles[0][0].isoformat() if candles else "NONE"
        last = candles[-1][0].isoformat() if candles else "NONE"
        status = "PASS" if count >= MIN_CANDLES else "FAIL"

        print(
            f"[RESULT] {label} | {timeframe} | "
            f"rows={len(rows)} | closed_valid={count} | "
            f"first={first} | last={last} | {status}"
        )
        return status == "PASS"

    except Exception as exc:
        print(f"[ERROR] {label} | {timeframe}: {exc}")
        return False


def main():
    print("=== LSE US500 / SPSPX.IDX DIAGNOSTIC ===")
    print("Market:", MARKET)
    print("Symbol:", SYMBOL)
    print("Minimum closed candles:", MIN_CANDLES)

    if not API_KEY:
        raise RuntimeError("LSE_API_KEY secret topilmadi")

    candidate = find_candidate()
    if candidate is None:
        print("[STOP] Katalogda aniq tasdiq topilmadi.")
        return

    results = {}
    for label, timeframe in TIMEFRAMES.items():
        results[label] = export_timeframe(label, timeframe)

    passed = sum(results.values())
    print("\n=== FINAL SUMMARY ===")
    for label, ok in results.items():
        print(f"{label}: {'PASS' if ok else 'FAIL'}")

    print(f"TOTAL: {passed}/5 timeframe passed")

    if passed == 5:
        print("[DECISION] SPSPX.IDX barcha timeframe talabidan o'tdi.")
    else:
        print("[DECISION] SPSPX.IDX universal US500 manbasi sifatida o'tmadi.")


if __name__ == "__main__":
    main()
          
