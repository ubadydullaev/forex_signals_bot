
import unittest
from unittest.mock import patch

from v6.config import TIMEFRAMES
from v6.mtf_analysis import (
    analyze_mtf,
    TIMEFRAME_WEIGHTS,
    ALIGNMENT_TIMEFRAMES,
)


class TestV6MTFAnalysis(unittest.TestCase):

    def setUp(self):
        self.candles = {
            timeframe: [object()]
            for timeframe in TIMEFRAMES
        }

    @staticmethod
    def result(timeframe, trend):
        return {
            "timeframe": timeframe,
            "trend": trend,
            "strength": 25.0,
            "rsi": 55.0,
            "adx": 25.0,
            "ema_20": 3.0,
            "ema_50": 2.0,
            "ema_200": 1.0,
            "supertrend_direction": 1,
            "structure_trend": trend,
            "reasons": [],
        }

    def run_with_trends(self, trends):

        def fake_analysis(candles, timeframe):
            return self.result(
                timeframe,
                trends.get(timeframe, "UNKNOWN"),
            )

        with patch(
            "v6.mtf_analysis.analyze_single_timeframe",
            side_effect=fake_analysis,
        ):
            return analyze_mtf(self.candles)

    def test_total_configured_weight(self):
        result = self.run_with_trends({
            timeframe: "BULLISH"
            for timeframe in TIMEFRAMES
        })

        expected = sum(
            TIMEFRAME_WEIGHTS[timeframe]
            for timeframe in ALIGNMENT_TIMEFRAMES
        )

        self.assertEqual(result["total_weight"], expected)
        self.assertEqual(result["alignment_percent"], 100.0)

    def test_higher_timeframes_and_m30_confirm(self):
        result = self.run_with_trends({
            "D1": "BULLISH",
            "H4": "BULLISH",
            "H1": "BULLISH",
            "M30": "BULLISH",
            "M15": "BEARISH",
        })

        self.assertEqual(result["overall_trend"], "BULLISH")
        self.assertTrue(result["higher_confirmation"])
        self.assertTrue(result["lower_confirmation"])
        self.assertEqual(result["alignment_percent"], 100.0)

    def test_m30_opposition_prevents_lower_confirmation(self):
        result = self.run_with_trends({
            "D1": "BULLISH",
            "H4": "BULLISH",
            "H1": "BULLISH",
            "M30": "BEARISH",
            "M15": "BULLISH",
        })

        self.assertEqual(result["overall_trend"], "BULLISH")
        self.assertTrue(result["higher_confirmation"])
        self.assertFalse(result["lower_confirmation"])

        self.assertAlmostEqual(
            result["alignment_percent"],
            8.0 / 9.0 * 100.0,
            places=2,
        )

    def test_higher_timeframe_disagreement_blocks_confirmation(self):
        result = self.run_with_trends({
            "D1": "BULLISH",
            "H4": "BULLISH",
            "H1": "BEARISH",
            "M30": "BULLISH",
            "M15": "BULLISH",
        })

        self.assertFalse(result["higher_confirmation"])

    def test_unknown_timeframe_does_not_inflate_alignment(self):
        result = self.run_with_trends({
            "D1": "BULLISH",
            "H4": "BULLISH",
            "H1": "BULLISH",
            "M30": "BULLISH",
            "M15": "UNKNOWN",
        })

        self.assertEqual(result["alignment_percent"], 100.0)

    def test_all_unknown_returns_unknown(self):
        result = self.run_with_trends({})

        self.assertEqual(result["overall_trend"], "UNKNOWN")
        self.assertEqual(result["alignment_percent"], 0.0)
        self.assertFalse(result["higher_confirmation"])
        self.assertFalse(result["lower_confirmation"])


if __name__ == "__main__":
    unittest.main()
