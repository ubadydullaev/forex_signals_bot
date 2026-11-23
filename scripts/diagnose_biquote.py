
import time
import requests

API_URL = "https://biquote.io/api"

SYMBOLS = [
    "EURUSD",
    "GBPUSD",
    "XAUUSD",
    "XAGUSD",
    "US500",
]

TIMEFRAMES = {
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
    "D1": "1d",
}

LIMIT = 1000

session = requests.Session()
session.headers.update({
    "User-Agent": "ForexSignalsBot-V6-Diagnostic/1.0"
})

success = 0
failed = 0

print("=" * 75)
print("BIQUOTE API DIAGNOSTIC")
print("Symbols:", ", ".join(SYMBOLS))
print("Timeframes:", ", ".join(TIMEFRAMES))
print("Requested candles:", LIMIT)
print("=" * 75)

# First: check exact symbol availability
print("\nSYMBOL AVAILABILITY")

for symbol in SYMBOLS:
    try:
        response = session.get(
            f"{API_URL}/symbols/{symbol}",
            timeout=20,
        )

        print(f"\n[{symbol}] HTTP: {response.status_code}")
        print("Response:", response.text[:500])

    except requests.RequestException as error:
        print(f"[{symbol}] REQUEST ERROR: {error}")

    time.sleep(0.5)

# Second: test OHLC data
print("\n" + "=" * 75)
print("OHLC DATA TEST")
print("=" * 75)

for symbol in SYMBOLS:
    for timeframe, interval in TIMEFRAMES.items():

        url = f"{API_URL}/{symbol}/ohlc"

        params = {
            "interval": interval,
            "limit": LIMIT,
        }

        print(f"\n[{symbol} | {timeframe}]")

        try:
            response = session.get(
                url,
                params=params,
                timeout=30,
            )

            print("HTTP:", response.status_code)

            if response.status_code != 200:
                print("ERROR:", response.text[:400])
                failed += 1
                time.sleep(0.5)
                continue

            payload = response.json()

            if not isinstance(payload, dict):
                print("INVALID JSON STRUCTURE")
                failed += 1
                continue

            bars = payload.get("bars", [])

            if not isinstance(bars, list):
                print("BARS FIELD IS NOT A LIST")
                print("Keys:", list(payload.keys()))
                failed += 1
                continue

            closed = [
                bar for bar in bars
                if isinstance(bar, dict)
                and bar.get("isOpen") is False
            ]

            opened = [
                bar for bar in bars
                if isinstance(bar, dict)
                and bar.get("isOpen") is True
            ]

            print("API symbol:", payload.get("symbol"))
            print("API interval:", payload.get("interval"))
            print("Total bars:", len(bars))
            print("Closed bars:", len(closed))
            print("Open bars:", len(opened))

            if bars:
                print("First bar:", bars[0])
                print("Last bar:", bars[-1])

            if not closed:
                print("NO CLOSED CANDLES")
                failed += 1
                continue

            required = [
                "openTime",
                "open",
                "high",
                "low",
                "close",
            ]

            missing = [
                key for key in required
                if key not in closed[0]
            ]

            if missing:
                print("MISSING FIELDS:", missing)
                failed += 1
                continue

            if len(closed) < 220:
                print("NOT ENOUGH CLOSED CANDLES")
                failed += 1
                continue

            print("OHLC STRUCTURE: OK")
            print("MINIMUM 220: PASSED")
            success += 1

        except requests.RequestException as error:
            print("REQUEST ERROR:", repr(error))
            failed += 1

        except ValueError as error:
            print("JSON ERROR:", repr(error))
            failed += 1

        except Exception as error:
            print("UNEXPECTED ERROR:", repr(error))
            failed += 1

        time.sleep(0.5)

print("\n" + "=" * 75)
print("BIQUOTE DIAGNOSTIC FINISHED")
print("Passed:", success)
print("Failed:", failed)
print("=" * 75)

print(
    "IMPORTANT: Verify timestamps, data freshness, "
    "and source consistency before production use."
      )
