
import unittest

from v6.models import Candle
from v6.indicators import (
    ema,
    rsi,
    atr,
    macd,
    adx,
    vwap,
    supertrend,
    calculate_indicators,
)


class TestV6Indicators(unittest.TestCase):

    @staticmethod
    def make_candles(prices, volumes=None):
        if volumes is None:
            volumes = [100.0] * len(prices)

        candles = []

        for i, price in enumerate(prices):
            candles.append(
                Candle(
                    timestamp=1760000000000 + i * 60000,
                    open=float(price),
                    high=float(price) + 2.0,
                    low=float(price) - 2.0,
                    close=float(price),
                    volume=float(volumes[i]),
                    is_closed=True,
                    source="test",
                )
            )

        return candles

    # EMA TESTS

    def test_ema_calculation(self):
        result = ema([10, 11, 12, 13, 14], 3)

        expected = [
            10.0,
            10.5,
            11.25,
            12.125,
            13.0625,
        ]

        self.assertEqual(len(result), len(expected))

        for actual, target in zip(result, expected):
            self.assertAlmostEqual(actual, target)

    def test_ema_empty_input(self):
        self.assertEqual(ema([], 20), [])

    def test_ema_invalid_period(self):
        with self.assertRaises(ValueError):
            ema([1, 2, 3], 0)

    # RSI TESTS

    def test_rsi_constant_market(self):
        result = rsi([100.0] * 30, 14)

        self.assertIsNone(result[13])
        self.assertAlmostEqual(result[14], 50.0)
        self.assertAlmostEqual(result[-1], 50.0)

    def test_rsi_rising_market(self):
        result = rsi(list(range(100, 121)), 14)

        self.assertAlmostEqual(result[-1], 100.0)

    def test_rsi_falling_market(self):
        result = rsi(list(range(120, 99, -1)), 14)

        self.assertAlmostEqual(result[-1], 0.0)

    def test_rsi_insufficient_data(self):
        result = rsi([100, 101, 102], 14)

        self.assertTrue(all(value is None for value in result))

    def test_rsi_invalid_period(self):
        with self.assertRaises(ValueError):
            rsi([1, 2, 3], 0)

    # ATR TESTS

    def test_atr_wilder_calculation(self):
        candles = self.make_candles([100, 101, 102, 103, 104])

        result = atr(candles, 2)

        self.assertIsNone(result[0])
        self.assertAlmostEqual(result[1], 4.0)
        self.assertAlmostEqual(result[2], 4.0)
        self.assertEqual(len(result), len(candles))

    def test_atr_accounts_for_price_gaps(self):
        candles = self.make_candles([100, 110, 120])

        result = atr(candles, 2)

        # First true range = 4
        # Second true range = 12
        # Initial Wilder average = (4 + 12) / 2
        self.assertAlmostEqual(result[1], 8.0)

    def test_atr_invalid_period(self):
        with self.assertRaises(ValueError):
            atr(self.make_candles([100, 101, 102]), 0)

    # MACD TESTS

    def test_macd_returns_all_series(self):
        prices = [100 + i for i in range(100)]
        result = macd(prices)

        for key in ("macd", "signal", "histogram"):
            self.assertIn(key, result)
            self.assertEqual(len(result[key]), len(prices))

    def test_macd_histogram_calculation(self):
        prices = [100 + i * 0.5 for i in range(100)]
        result = macd(prices)

        for main, signal, histogram in zip(
            result["macd"],
            result["signal"],
            result["histogram"],
        ):
            if main is not None and signal is not None:
                self.assertAlmostEqual(
                    histogram,
                    main - signal,
                )

    def test_macd_bullish_momentum(self):
        prices = [100 + i for i in range(100)]
        result = macd(prices)

        self.assertGreater(result["macd"][-1], 0.0)
        self.assertGreater(result["histogram"][-1], 0.0)

    def test_macd_invalid_periods(self):
        with self.assertRaises(ValueError):
            macd([1, 2, 3], fast=26, slow=12)

        with self.assertRaises(ValueError):
            macd([1, 2, 3], signal_period=0)

    # ADX TESTS

    def test_adx_constant_market(self):
        candles = self.make_candles([100.0] * 40)
        result = adx(candles, 3)

        self.assertEqual(len(result["adx"]), len(candles))
        self.assertAlmostEqual(result["adx"][-1], 0.0)
        self.assertAlmostEqual(result["plus_di"][-1], 0.0)
        self.assertAlmostEqual(result["minus_di"][-1], 0.0)

    def test_adx_bullish_directional_movement(self):
        candles = self.make_candles(
            [100, 102, 104, 106, 108, 110, 112, 114, 116, 118]
        )

        result = adx(candles, 3)

        self.assertGreater(
            result["plus_di"][-1],
            result["minus_di"][-1],
        )

        self.assertIsNotNone(result["adx"][-1])

    def test_adx_returns_all_series(self):
        candles = self.make_candles(
            [100 + i for i in range(50)]
        )

        result = adx(candles, 14)

        for key in ("adx", "plus_di", "minus_di"):
            self.assertIn(key, result)
            self.assertEqual(len(result[key]), len(candles))

    def test_adx_invalid_period(self):
        with self.assertRaises(ValueError):
            adx(self.make_candles([100, 101, 102]), 0)

    # VWAP TESTS

    def test_vwap_resets_at_session_boundary(self):
        candles = self.make_candles(
            [100, 110, 120, 130],
            volumes=[1, 1, 1, 1],
        )

        result = vwap(
            candles,
            session_keys=["day1", "day1", "day2", "day2"],
        )

        self.assertAlmostEqual(result[0], 100.0)
        self.assertAlmostEqual(result[1], 105.0)
        self.assertAlmostEqual(result[2], 120.0)
        self.assertAlmostEqual(result[3], 125.0)

    def test_vwap_rejects_wrong_session_key_length(self):
        candles = self.make_candles([100, 110, 120])

        with self.assertRaises(ValueError):
            vwap(candles, session_keys=["day1", "day2"])

    def test_vwap_rejects_negative_volume(self):
        candles = self.make_candles(
            [100, 110],
            volumes=[100, -10],
        )

        with self.assertRaises(ValueError):
            vwap(candles)
            
    def test_vwap_volume_weighting(self):
        candles = self.make_candles(
            [100, 110],
            volumes=[1, 3],
        )

        result = vwap(candles)

        expected = (100.0 * 1 + 110.0 * 3) / 4

        self.assertAlmostEqual(result[0], 100.0)
        self.assertAlmostEqual(result[-1], expected)

    def test_vwap_zero_volume(self):
        candles = self.make_candles(
            [100, 110],
            volumes=[0, 0],
        )

        self.assertEqual(vwap(candles), [None, None])

    def test_vwap_returns_correct_length(self):
        candles = self.make_candles([100, 101, 102, 103])

        self.assertEqual(len(vwap(candles)), len(candles))

    # SUPERTREND TESTS

    def test_supertrend_returns_all_series(self):
        candles = self.make_candles(
            [100, 110, 140, 150, 160, 170]
        )

        result = supertrend(candles, period=2)

        for key in (
            "supertrend",
            "direction",
            "upper_band",
            "lower_band",
        ):
            self.assertIn(key, result)
            self.assertEqual(len(result[key]), len(candles))

    def test_supertrend_initial_direction_is_bearish(self):
        candles = self.make_candles(
            [100, 110, 120, 130]
        )

        result = supertrend(candles, period=2)

        self.assertIsNone(result["direction"][0])
        self.assertEqual(result["direction"][1], -1)

    def test_supertrend_bullish_breakout(self):
        candles = self.make_candles(
            [100, 110, 140, 150, 160, 170]
        )

        result = supertrend(candles, period=2)

        self.assertEqual(result["direction"][2], 1)

    def test_supertrend_invalid_settings(self):
        candles = self.make_candles([100, 110, 120])

        with self.assertRaises(ValueError):
            supertrend(candles, period=0)

        with self.assertRaises(ValueError):
            supertrend(candles, multiplier=0)

    # COMPLETE INDICATOR OUTPUT

    def test_calculate_indicators_output(self):
        prices = [100 + i * 0.1 for i in range(250)]
        candles = self.make_candles(prices)

        result = calculate_indicators(candles)

        expected_keys = (
            "ema_20",
            "ema_50",
            "ema_200",
            "rsi_14",
            "atr_14",
            "macd",
            "adx",
            "vwap",
            "supertrend",
        )

        for key in expected_keys:
            self.assertIn(key, result)

        for key in (
            "ema_20",
            "ema_50",
            "ema_200",
            "rsi_14",
            "atr_14",
            "vwap",
        ):
            self.assertEqual(
                len(result[key]),
                len(candles),
            )

        self.assertEqual(
            len(result["macd"]["macd"]),
            len(candles),
        )

        self.assertEqual(
            len(result["adx"]["adx"]),
            len(candles),
        )

        self.assertEqual(
            len(result["supertrend"]["direction"]),
            len(candles),
        )


if __name__ == "__main__":
    unittest.main()
