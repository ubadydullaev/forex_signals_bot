
import unittest
from unittest.mock import patch

from v6.config import TIMEFRAMES
from v6.models import Candle
from v6.signal_engine import (
    analyze_symbol,
    _check_direction,
)


class TestV6SignalEngine(unittest.TestCase):

    SYMBOL = "EURUSD"
    CANDLE_COUNT = 200

    def make_candles(self, count=200, closed=True):
        candles = []

        for i in range(count):
            close = 100.0

            candles.append(
                Candle(
                    timestamp=1760000000000 + i * 900000,
                    open=close,
                    high=101.0,
                    low=99.0,
                    close=close,
                    volume=100.0,
                    is_closed=closed,
                    source="test",
                )
            )

        return candles

    def make_market_data(self):
        return {
            timeframe: self.make_candles()
            for timeframe in TIMEFRAMES
        }

    def make_mtf(self, trend="BULLISH", alignment=100.0):
        return {
            "overall_trend": trend,
            "alignment_percent": alignment,
            "higher_confirmation": True,
            "lower_confirmation": True,
        }

    def make_indicators(
        self,
        direction="BULLISH",
        rsi_value=None,
        adx_value=25.0,
        atr_value=2.0,
    ):
        if rsi_value is None:
            rsi_value = (
                60.0 if direction == "BULLISH" else 40.0
            )

        supertrend_direction = (
            1 if direction == "BULLISH" else -1
        )

        if direction == "BULLISH":
            macd_data = {
                "macd": [2.0],
                "signal": [1.0],
                "histogram": [1.0],
            }
        else:
            macd_data = {
                "macd": [-2.0],
                "signal": [-1.0],
                "histogram": [-1.0],
            }

        return {
            "supertrend": {
                "direction": [supertrend_direction],
            },
            "rsi_14": [rsi_value],
            "adx": {
                "adx": [adx_value],
            },
            "macd": macd_data,
            "atr_14": [atr_value],
        }

    def make_structure(self, direction="BULLISH"):
        return {
            "trend": direction,
            "events": [],
            "liquidity_sweeps": [],
        }

    def run_analysis(
        self,
        direction="BULLISH",
        alignment=100.0,
        indicators=None,
        structure=None,
    ):
        market_data = self.make_market_data()

        if indicators is None:
            indicators = self.make_indicators(direction)

        if structure is None:
            structure = self.make_structure(direction)

        with (
            patch(
                "v6.signal_engine.analyze_mtf",
                return_value=self.make_mtf(
                    direction,
                    alignment,
                ),
            ),
            patch(
                "v6.signal_engine.calculate_indicators",
                return_value=indicators,
            ),
            patch(
                "v6.signal_engine.analyze_market_structure",
                return_value=structure,
            ),
        ):
            return analyze_symbol(
                self.SYMBOL,
                market_data,
            )

    # ======================================
    # BUY / SELL SIGNAL TESTS
    # ======================================

    def test_valid_bullish_conditions_generate_buy(self):
        result = self.run_analysis("BULLISH")

        self.assertEqual(result.signal.side, "BUY")
        self.assertEqual(result.signal.timeframe, "M15")
        self.assertIsNotNone(result.signal.entry)
        self.assertLess(
            result.signal.stop_loss,
            result.signal.entry,
        )
        self.assertGreater(
            result.signal.take_profit,
            result.signal.entry,
        )
        self.assertGreaterEqual(
            result.signal.risk_reward,
            1.5,
        )

    def test_valid_bearish_conditions_generate_sell(self):
        result = self.run_analysis("BEARISH")

        self.assertEqual(result.signal.side, "SELL")
        self.assertEqual(result.signal.timeframe, "M15")
        self.assertGreater(
            result.signal.stop_loss,
            result.signal.entry,
        )
        self.assertLess(
            result.signal.take_profit,
            result.signal.entry,
        )
        self.assertGreaterEqual(
            result.signal.risk_reward,
            1.5,
        )

    # ======================================
    # MARKET DATA VALIDATION
    # ======================================

    def test_unclosed_candle_returns_hold(self):
        market_data = self.make_market_data()
        market_data["M15"][-1].is_closed = False

        result = analyze_symbol(
            self.SYMBOL,
            market_data,
        )

        self.assertEqual(result.signal.side, "HOLD")
        self.assertIn("M15", result.signal.reason)

    def test_insufficient_candles_returns_hold(self):
        market_data = self.make_market_data()
        market_data["H1"] = self.make_candles(100)

        result = analyze_symbol(
            self.SYMBOL,
            market_data,
        )

        self.assertEqual(result.signal.side, "HOLD")
        self.assertIn("H1", result.signal.reason)

    # ======================================
    # MTF FILTER TESTS
    # ======================================

    def test_mtf_unknown_direction_returns_hold(self):
        result = self.run_analysis("UNKNOWN")

        self.assertEqual(result.signal.side, "HOLD")

    def test_low_alignment_returns_hold(self):
        result = self.run_analysis(
            "BULLISH",
            alignment=70.0,
        )

        self.assertEqual(result.signal.side, "HOLD")
        self.assertIn("alignment", result.signal.reason.lower())

    def test_higher_timeframe_disagreement_returns_hold(self):
        market_data = self.make_market_data()

        mtf = self.make_mtf()
        mtf["higher_confirmation"] = False

        with patch(
            "v6.signal_engine.analyze_mtf",
            return_value=mtf,
        ):
            result = analyze_symbol(
                self.SYMBOL,
                market_data,
            )

        self.assertEqual(result.signal.side, "HOLD")

    def test_m30_disagreement_returns_hold(self):
        market_data = self.make_market_data()

        mtf = self.make_mtf()
        mtf["lower_confirmation"] = False

        with patch(
            "v6.signal_engine.analyze_mtf",
            return_value=mtf,
        ):
            result = analyze_symbol(
                self.SYMBOL,
                market_data,
            )

        self.assertEqual(result.signal.side, "HOLD")

    # ======================================
    # M15 INDICATOR FILTER TESTS
    # ======================================

    def test_wrong_supertrend_returns_hold(self):
        indicators = self.make_indicators("BULLISH")
        indicators["supertrend"]["direction"] = [-1]

        result = self.run_analysis(
            "BULLISH",
            indicators=indicators,
        )

        self.assertEqual(result.signal.side, "HOLD")
        self.assertIn("Supertrend", result.signal.reason)

    def test_invalid_buy_rsi_returns_hold(self):
        indicators = self.make_indicators(
            "BULLISH",
            rsi_value=80.0,
        )

        result = self.run_analysis(
            "BULLISH",
            indicators=indicators,
        )

        self.assertEqual(result.signal.side, "HOLD")
        self.assertIn("RSI", result.signal.reason)

    def test_invalid_sell_rsi_returns_hold(self):
        indicators = self.make_indicators(
            "BEARISH",
            rsi_value=20.0,
        )

        result = self.run_analysis(
            "BEARISH",
            indicators=indicators,
        )

        self.assertEqual(result.signal.side, "HOLD")
        self.assertIn("RSI", result.signal.reason)

    def test_weak_adx_returns_hold(self):
        indicators = self.make_indicators(
            "BULLISH",
            adx_value=15.0,
        )

        result = self.run_analysis(
            "BULLISH",
            indicators=indicators,
        )

        self.assertEqual(result.signal.side, "HOLD")
        self.assertIn("ADX", result.signal.reason)

    def test_macd_disagreement_returns_hold(self):
        indicators = self.make_indicators("BULLISH")
        indicators["macd"] = {
            "macd": [-2.0],
            "signal": [-1.0],
            "histogram": [-1.0],
        }

        result = self.run_analysis(
            "BULLISH",
            indicators=indicators,
        )

        self.assertEqual(result.signal.side, "HOLD")
        self.assertIn("MACD", result.signal.reason)

    def test_missing_atr_returns_hold(self):
        indicators = self.make_indicators(
            "BULLISH",
            atr_value=None,
        )

        result = self.run_analysis(
            "BULLISH",
            indicators=indicators,
        )

        self.assertEqual(result.signal.side, "HOLD")
        self.assertIn("ATR", result.signal.reason)

    def test_market_structure_without_confirmation_returns_hold(self):
        structure = self.make_structure("UNKNOWN")

        result = self.run_analysis(
            "BULLISH",
            structure=structure,
        )

        self.assertEqual(result.signal.side, "HOLD")
        self.assertIn("BOS/CHOCH", result.signal.reason)

    # ======================================
    # DIRECT DIRECTION CHECK TESTS
    # ======================================

    def test_direction_check_rejects_unknown_direction(self):
        indicators = self.make_indicators("BULLISH")
        structure = self.make_structure("BULLISH")
        candles = self.make_candles()

        confirmed, reasons = _check_direction(
            "UNKNOWN",
            indicators,
            structure,
            candles,
        )

        self.assertFalse(confirmed)
        self.assertTrue(reasons)

    def test_direction_check_accepts_bullish_confirmations(self):
        indicators = self.make_indicators("BULLISH")
        structure = self.make_structure("BULLISH")
        candles = self.make_candles()

        confirmed, reasons = _check_direction(
            "BULLISH",
            indicators,
            structure,
            candles,
        )

        self.assertTrue(confirmed)
        self.assertTrue(reasons)

    def test_direction_check_accepts_bearish_confirmations(self):
        indicators = self.make_indicators("BEARISH")
        structure = self.make_structure("BEARISH")
        candles = self.make_candles()

        confirmed, reasons = _check_direction(
            "BEARISH",
            indicators,
            structure,
            candles,
        )

        self.assertTrue(confirmed)
        self.assertTrue(reasons)


if __name__ == "__main__":
    unittest.main()
      
