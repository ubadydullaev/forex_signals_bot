
import unittest
from unittest.mock import patch

from v6.mtf_analysis import analyze_single_timeframe


class TestV6MTFConfirmation(unittest.TestCase):

    @staticmethod
    def make_indicators(
        ema_values,
        supertrend_direction,
        adx_value=25.0,
    ):
        return {
            "ema_20": [ema_values[0]],
            "ema_50": [ema_values[1]],
            "ema_200": [ema_values[2]],
            "rsi_14": [55.0],
            "adx": {"adx": [adx_value]},
            "supertrend": {
                "direction": [supertrend_direction]
            },
        }

    def run_analysis(
        self,
        ema_values,
        supertrend_direction,
    ):
        indicators = self.make_indicators(
            ema_values,
            supertrend_direction,
        )

        structure = {
            "trend": "UNKNOWN",
            "swings": [],
            "events": [],
            "liquidity_sweeps": [],
            "levels": {
                "support": None,
                "resistance": None,
            },
        }

        with (
            patch(
                "v6.mtf_analysis.calculate_indicators",
                return_value=indicators,
            ),
            patch(
                "v6.mtf_analysis.analyze_market_structure",
                return_value=structure,
            ),
        ):
            return analyze_single_timeframe(
                [object()],
                "H1",
            )

    def test_bullish_ema_and_supertrend_confirm(self):
        result = self.run_analysis(
            (120.0, 110.0, 100.0),
            1,
        )

        self.assertEqual(result["trend"], "BULLISH")
        self.assertTrue(result["supertrend_confirmed"])

    def test_bearish_ema_and_supertrend_confirm(self):
        result = self.run_analysis(
            (80.0, 90.0, 100.0),
            -1,
        )

        self.assertEqual(result["trend"], "BEARISH")
        self.assertTrue(result["supertrend_confirmed"])

    def test_bullish_ema_bearish_supertrend_rejected(self):
        result = self.run_analysis(
            (120.0, 110.0, 100.0),
            -1,
        )

        self.assertEqual(result["trend"], "RANGING")
        self.assertFalse(result["supertrend_confirmed"])

    def test_bearish_ema_bullish_supertrend_rejected(self):
        result = self.run_analysis(
            (80.0, 90.0, 100.0),
            1,
        )

        self.assertEqual(result["trend"], "RANGING")
        self.assertFalse(result["supertrend_confirmed"])

    def test_mixed_ema_alignment_rejected(self):
        result = self.run_analysis(
            (100.0, 110.0, 105.0),
            1,
        )

        self.assertEqual(result["trend"], "RANGING")
        self.assertFalse(result["supertrend_confirmed"])

    def test_missing_supertrend_does_not_confirm(self):
        result = self.run_analysis(
            (120.0, 110.0, 100.0),
            None,
        )

        self.assertEqual(result["trend"], "UNKNOWN")
        self.assertFalse(result["supertrend_confirmed"])


if __name__ == "__main__":
    unittest.main()
      
