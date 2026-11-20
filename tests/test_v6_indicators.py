
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
)


class TestV6Indicators(unittest.TestCase):

    def make_candles(self, prices, volumes=None):
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

    def test_ema_returns_correct_length(self):
        result = ema([10, 11, 12, 13, 14], 3)

        self.assertEqual(len(result), 5)
        self.assertAlmostEqual(result[0], 10.0)
        self.assertAlmostEqual(result[1], 10.5)
        self.assertAlmostEqual(result[2], 11.25)

    def test_ema_rejects_invalid_period(self):
        with self.assertRaises(ValueError):
            ema([1, 2, 3], 0)

    def test_rsi_constant_market(self):
        result = rsi([100.0] * 20, 14)
        self.assertEqual(result[14], 50.0)

    def test_rsi_rising_market(self):
        result = rsi(list(range(100, 121)), 14)
        self.assertAlmostEqual(result[-1], 100.0)

    def test_rsi_falling_market(self):
        result = rsi(list(range(120, 99, -1)), 14)
        self.assertAlmostEqual(result[-1], 0.0)

    def test_atr_returns_correct_length(self):
        candles = self.make_candles(
            [100, 101, 102, 103, 104, 105]
        )

        result = atr(candles, 3)

        self.assertEqual(len(result), len(candles))
        self.assertIsNone(result[1])
        self.assertIsNotNone(result[2])

    def test_atr_rejects_invalid_period(self):
        with self.assertRaises(ValueError):
            atr(self.make_candles([100, 101, 102]), 0)

    def test_macd_returns_three_series(self):
        prices = [100 + i for i in range(60)]
        result = macd(prices)

        for key in ("macd", "signal", "histogram"):
            self.assertIn(key, result)
            self.assertEqual(len(result[key]), len(prices))

    def test_macd_rejects_invalid_periods(self):
        with self.assertRaises(ValueError):
            macd([1, 2, 3], fast=26, slow=12)

    def test_adx_constant_market_is_zero(self):
        candles = self.make_candles([100.0] * 20)
        result = adx(candles, 3)

        self.assertEqual(len(result["adx"]), len(candles))
        self.assertAlmostEqual(result["adx"][-1], 0.0)
        self.assertAlmostEqual(result["plus_di"][-1], 0.0)
        self.assertAlmostEqual(result["minus_di"][-1], 0.0)

    def test_adx_returns_directional_series(self):
        candles = self.make_candles(
            [100, 102, 104, 106, 108, 110, 112, 114, 116, 118]
        )
        result = adx(candles, 3)

        self.assertEqual(len(result["plus_di"]), len(candles))
        self.assertEqual(len(result["minus_di"]), len(candles))
        self.assertIsNotNone(result["adx"][-1])
        self.assertGreater(
            result["plus_di"][-1],
            result["minus_di"][-1],
        )

    def test_vwap_uses_volume_weighting(self):
        candles = self.make_candles(
            [100, 110],
            volumes=[1, 3],
        )

        result = vwap(candles)

        first_typical = (102 + 98 + 100) / 3
        second_typical = (112 + 108 + 110) / 3
        expected = (
            first_typical * 1 + second_typical * 3
        ) / 4

        self.assertAlmostEqual(result[-1], expected)

    def test_vwap_zero_volume_returns_none(self):
        candles = self.make_candles(
            [100, 110],
            volumes=[0, 0],
        )

        self.assertEqual(vwap(candles), [None, None])

    def test_supertrend_returns_all_series(self):
        candles = self.make_candles(
            [100, 110, 120, 130, 140, 150]
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
        candles = self.make_candles([100, 110, 120, 130])
        result = supertrend(candles, period=2)

        self.assertEqual(result["direction"][1], -1)

    def test_supertrend_confirms_bullish_breakout(self):
        candles = self.make_candles(
            [100, 110, 120, 130, 140, 150]
        )
        result = supertrend(candles, period=2)

        self.assertEqual(result["direction"][2], 1)

    def test_supertrend_rejects_invalid_settings(self):
        candles = self.make_candles([100, 110, 120])

        with self.assertRaises(ValueError):
            supertrend(candles, period=0)

        with self.assertRaises(ValueError):
            supertrend(candles, multiplier=0)


if __name__ == "__main__":
    unittest.main()
