
import unittest
import warnings
from datetime import datetime, timezone
from unittest.mock import patch

from v6 import data_adapter


class TestV6DataAdapter(unittest.TestCase):

    @staticmethod
    def timestamp(year, month, day, hour, minute):
        return int(
            datetime(
                year, month, day, hour, minute,
                tzinfo=timezone.utc,
            ).timestamp() * 1000
        )

    @staticmethod
    def candle(timestamp):
        return data_adapter.Candle(
            timestamp=timestamp,
            open=2000,
            high=2005,
            low=1995,
            close=2001,
            volume=100,
            is_closed=True,
            source="dukascopy",
        )

    def inspect(self, candles, symbol="XAUUSD", timeframe="M15"):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            gaps = data_adapter._inspect_candle_gaps(
                candles,
                timeframe,
                "dukascopy",
                symbol=symbol,
            )
        return gaps, caught

    def test_gold_daily_break_is_ignored(self):
        first = self.timestamp(2026, 11, 16, 21, 45)
        second = self.timestamp(2026, 11, 16, 23, 0)

        gaps, caught = self.inspect(
            [self.candle(first), self.candle(second)],
            "XAUUSD",
        )

        self.assertEqual(gaps, [])
        self.assertEqual(len(caught), 0)

    def test_silver_daily_break_is_ignored(self):
        first = self.timestamp(2026, 11, 16, 21, 45)
        second = self.timestamp(2026, 11, 16, 23, 0)

        gaps, caught = self.inspect(
            [self.candle(first), self.candle(second)],
            "XAGUSD",
        )

        self.assertEqual(gaps, [])
        self.assertEqual(len(caught), 0)

    def test_removed_us500_is_rejected(self):
        with self.assertRaises(ValueError):
            data_adapter._validate_request(
                symbol="US500",
                timeframe="M15",
                limit=250,
            )

    def test_real_gold_weekday_gap_still_warns(self):
        first = self.timestamp(2026, 11, 17, 10, 0)
        second = self.timestamp(2026, 11, 17, 10, 45)

        gaps, caught = self.inspect(
            [self.candle(first), self.candle(second)],
            "XAUUSD",
        )

        self.assertEqual(len(gaps), 1)
        self.assertEqual(len(caught), 1)
        self.assertEqual(gaps[0]["missing_intervals"], 2)

    def test_dukascopy_weekend_gap_is_ignored(self):
        friday = self.timestamp(2026, 11, 20, 22, 0)
        sunday = self.timestamp(2026, 11, 22, 23, 0)

        gaps, caught = self.inspect(
            [self.candle(friday), self.candle(sunday)],
            "XAUUSD",
        )

        self.assertEqual(gaps, [])
        self.assertEqual(len(caught), 0)

    def test_binance_weekday_gap_generates_warning(self):
        first = self.timestamp(2026, 11, 17, 10, 0)
        second = first + 45 * 60 * 1000

        candles = [
            data_adapter.Candle(
                timestamp=first,
                open=2000,
                high=2005,
                low=1995,
                close=2001,
                volume=100,
                is_closed=True,
                source="binance_spot",
            ),
            data_adapter.Candle(
                timestamp=second,
                open=2001,
                high=2006,
                low=1998,
                close=2004,
                volume=100,
                is_closed=True,
                source="binance_spot",
            ),
        ]

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            gaps = data_adapter._inspect_candle_gaps(
                candles,
                "M15",
                "binance_spot",
                symbol="BTCUSD",
            )

        self.assertEqual(len(gaps), 1)
        self.assertEqual(len(caught), 1)

    def make_raw_candles(self, count=3):
        base_timestamp = 1700000000000
        duration = 15 * 60 * 1000
        candles = []

        for i in range(count):
            price = 2000.0 + i
            candles.append({
                "timestamp": base_timestamp + i * duration,
                "open": price,
                "high": price + 5.0,
                "low": price - 5.0,
                "close": price + 2.0,
                "volume": 100.0,
            })

        return candles

    def build(self, raw, limit=3):
        return data_adapter._build_candles(
            raw_candles=raw,
            timeframe="M15",
            source="binance_spot",
            limit=limit,
            symbol="BTCUSD",
        )

    def test_valid_candles_are_returned(self):
        raw = self.make_raw_candles(3)

        with patch.object(data_adapter, "MIN_CANDLES", 3):
            result = self.build(raw)

        self.assertEqual(len(result), 3)
        self.assertTrue(all(c.is_closed for c in result))
        self.assertTrue(
            all(c.source == "binance_spot" for c in result)
        )

    def test_invalid_ohlc_candle_is_rejected(self):
        raw = self.make_raw_candles(3)
        raw[1]["high"] = raw[1]["low"] - 10

        with patch.object(data_adapter, "MIN_CANDLES", 2):
            result = self.build(raw, limit=2)

        self.assertEqual(len(result), 2)

    def test_negative_volume_candle_is_rejected(self):
        raw = self.make_raw_candles(3)
        raw[1]["volume"] = -100.0

        with patch.object(data_adapter, "MIN_CANDLES", 2):
            result = self.build(raw, limit=2)

        self.assertEqual(len(result), 2)

    def test_identical_duplicate_is_removed(self):
        raw = self.make_raw_candles(3)
        raw.append(raw[1].copy())

        with patch.object(data_adapter, "MIN_CANDLES", 3):
            result = self.build(raw)

        self.assertEqual(len(result), 3)

    def test_conflicting_duplicate_is_rejected(self):
        raw = self.make_raw_candles(3)
        duplicate = raw[1].copy()
        duplicate["close"] += 10
        duplicate["high"] += 20
        raw.append(duplicate)

        with patch.object(data_adapter, "MIN_CANDLES", 3):
            with self.assertRaises(RuntimeError):
                self.build(raw)

    def test_unclosed_future_candle_is_ignored(self):
        raw = self.make_raw_candles(3)
        import time

        future_timestamp = (
            int(time.time() * 1000)
            + 24 * 60 * 60 * 1000
        )

        raw.append({
            "timestamp": future_timestamp,
            "open": 2000.0,
            "high": 2010.0,
            "low": 1990.0,
            "close": 2005.0,
            "volume": 100.0,
        })

        with patch.object(data_adapter, "MIN_CANDLES", 3):
            result = self.build(raw)

        self.assertEqual(len(result), 3)

    def test_insufficient_candles_raise_error(self):
        raw = self.make_raw_candles(2)

        with patch.object(data_adapter, "MIN_CANDLES", 3):
            with self.assertRaises(RuntimeError):
                self.build(raw)

    def test_binance_gap_generates_warning(self):
        raw = self.make_raw_candles(3)
        duration = 15 * 60 * 1000
        raw[2]["timestamp"] += duration * 3

        with patch.object(data_adapter, "MIN_CANDLES", 3):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.build(raw)

        self.assertTrue(
            any("uzilish" in str(w.message) for w in caught)
        )

    def test_invalid_request_is_rejected(self):
        with self.assertRaises(ValueError):
            data_adapter._validate_request(
                symbol="UNKNOWN",
                timeframe="M15",
                limit=250,
            )

    def test_limit_below_minimum_is_rejected(self):
        with self.assertRaises(ValueError):
            data_adapter._validate_request(
                symbol="XAUUSD",
                timeframe="M15",
                limit=10,
            )


if __name__ == "__main__":
    unittest.main()
