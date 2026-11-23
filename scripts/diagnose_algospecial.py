
import time
from datetime import datetime, timedelta, timezone

import requests

API_URL = "https://www.algospecial.com/api/ohlc.php"

SYMBOLS = [
    "EURUSD",
    "GBPUSD",
    "XAUUSD",
    "XAGUSD",
    "US500",
]

TIMEFRAMES = [
    "M15",
    "M30",
    "H1",
    "H4",
    "D1",
]

session = requests.Session()
session.headers.update({
    "User-Agent": "ForexSignalsBot-V6-Diagnostic/1.0"
})

end_date = datetime.now(timezone.utc).date()
start_date = end_date - timedelta(days=30)

print("=" * 75)
print("ALGOSPECIAL API DIAGNOSTIC")
print(f"Date range: {start_date} -> {end_date}")
print("=" * 75)

success = 0
failed = 0

for symbol in SYMBOLS:
    for timeframe in TIMEFRAMES:

        params = {
            "symbol": symbol,
            "tf": timeframe,
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
            "format": "json",
        }

        try:
            response = session.get(
                API_URL,
                params=params,
                timeout=30,
            )

            print(f"\n[{symbol} | {timeframe}]")
            print("HTTP:", response.status_code)

            if response.status_code != 200:
                print("ERROR:", response.text[:300])
                failed += 1
                time.sleep(1)
                continue

            payload = response.json()

            if not isinstance(payload, dict):
                print("INVALID JSON STRUCTURE")
                failed += 1
                time.sleep(1)
                continue

            rows = payload.get("data", [])

            if not isinstance(rows, list):
                print("DATA FIELD IS NOT A LIST")
                print("Payload keys:", list(payload.keys()))
                failed += 1
                time.sleep(1)
                continue

            print("API symbol:", payload.get("symbol"))
            print("API timeframe:", payload.get("timeframe"))
            print("Reported rows:", payload.get("rows"))
            print("Received rows:", len(rows))

            if rows:
                print("First candle:", rows[0])
                print("Last candle:", rows[-1])

                required = [
                    "Timestamp",
                    "Open",
                    "High",
                    "Low",
                    "Close",
                ]

                missing = [
                    key for key in required
                    if key not in rows[0]
                ]

                if missing:
                    print("MISSING FIELDS:", missing)
                    failed += 1
                else:
                    print("OHLC STRUCTURE: OK")
                    success += 1
            else:
                print("NO CANDLES RETURNED")
                failed += 1

        except requests.RequestException as error:
            print("REQUEST ERROR:", error)
            failed += 1

        except ValueError as error:
            print("JSON ERROR:", error)
            failed += 1

        except Exception as error:
            print("UNEXPECTED ERROR:", repr(error))
            failed += 1

        time.sleep(1)

print("\n" + "=" * 75)
print("DIAGNOSTIC FINISHED")
print("Successful:", success)
print("Failed:", failed)
print("=" * 75)

print(
    "IMPORTANT: Timestamp timezone must be verified "
    "before using these candles in V6."
)
