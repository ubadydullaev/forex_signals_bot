
import unittest

from v6.models import Candle
from v6.indicators import (
    ema,
    rsi,
    atr,
    macd,
)


class TestV6Indicators(unittest.TestCase):

    def make_candles(self, prices):
        candles = []

        for i, price in enumerate(prices):
            candles.append(
                Candle(
                    timestamp=1760000000000 + i * 60000,
                    open=float(price),
                    high=float(price) + 2.0,
                    low=float(price) - 2.0,
                    close=float(price),
                    volume=100.0,
                    is_closed=True,
                    source="test",
                )
            )

        return candles

    def test_ema_returns_correct_length(self):
        values = [10, 11, 12, 13, 14]

        result = ema(values, 3)

        self.assertEqual(len(result), len(values))
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
        prices = list(range(100, 121))

        result = rsi(prices, 14)

        self.assertAlmostEqual(result[-1], 100.0)

    def test_rsi_falling_market(self):
        prices = list(range(120, 99, -1))

        result = rsi(prices, 14)

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
        candles = self.make_candles([100, 101, 102])

        with self.assertRaises(ValueError):
            atr(candles, 0)

    def test_macd_returns_three_series(self):
        prices = [100 + i for i in range(60)]

        result = macd(prices)

        self.assertIn("macd", result)
        self.assertIn("signal", result)
        self.assertIn("histogram", result)

        self.assertEqual(len(result["macd"]), len(prices))
        self.assertEqual(len(result["signal"]), len(prices))
        self.assertEqual(len(result["histogram"]), len(prices))

    def test_macd_rejects_invalid_periods(self):
        with self.assertRaises(ValueError):
            macd([1, 2, 3], fast=26, slow=12)


if __name__ == "__main__":
    unittest.main()
          
