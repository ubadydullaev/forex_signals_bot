
import unittest
from types import SimpleNamespace

from v6.models import Candle
from v6.signal_engine import (
    _analyze_macd_momentum,
    _check_direction,
)


def make_macd(histograms, direction="BEARISH"):
    signal = -1.0 if direction == "BEARISH" else 1.0

    macd_values = [
        signal + value
        for value in histograms
    ]

    return {
        "macd": macd_values,
        "signal": [signal] * len(histograms),
        "histogram": histograms,
    }


def make_candles(count=20):
    return [
        Candle(
            timestamp=1760000000000 + i * 900000,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=100.0,
            is_closed=True,
            source="test",
        )
        for i in range(count)
    ]


def make_indicators():
    return {
        "supertrend": {"direction": [1]},
        "rsi_14": [60.0],
        "adx": {"adx": [25.0]},
        "macd": {
            "macd": [2.0, 2.0, 2.0, 2.0],
            "signal": [1.0, 1.0, 1.0, 1.0],
            "histogram": [1.0, 1.0, 1.0, 1.0],
        },
        "atr_14": [2.0],
    }


class TestV6MacdMomentum(unittest.TestCase):

    def test_opposite_macd_weakening_is_accepted(self):
        macd = make_macd(
            [-4.0, -3.0, -2.5, -2.0],
            "BEARISH",
        )

        state, _ = _analyze_macd_momentum(macd, "BULLISH")

        self.assertEqual(state, "OPPOSING_WEAKENING")

    def test_opposite_macd_weakening_after_peak_is_accepted(self):
        macd = make_macd(
            [-2.6, -3.3, -2.8, -2.2],
            "BEARISH",
        )

        state, _ = _analyze_macd_momentum(macd, "BULLISH")

        self.assertEqual(state, "OPPOSING_WEAKENING")

    def test_opposite_macd_strengthening_is_rejected(self):
        macd = make_macd(
            [-1.0, -2.0, -3.0, -4.0],
            "BEARISH",
        )

        state, _ = _analyze_macd_momentum(macd, "BULLISH")

        self.assertEqual(state, "OPPOSING_STRENGTHENING")

    def test_small_change_is_unclear(self):
        macd = make_macd(
            [-3.0, -2.0, -2.5, -2.0],
            "BEARISH",
        )

        state, _ = _analyze_macd_momentum(macd, "BULLISH")

        self.assertEqual(state, "OPPOSING_UNCLEAR")

    def test_one_bar_decline_is_not_enough(self):
        macd = make_macd(
            [-1.0, -2.0, -3.0, -2.0],
            "BEARISH",
        )

        state, _ = _analyze_macd_momentum(macd, "BULLISH")

        self.assertEqual(state, "OPPOSING_UNCLEAR")

    def test_neutral_macd_is_accepted(self):
        macd = {
            "macd": [1.0, 1.0, 1.0, 1.0],
            "signal": [1.0, 1.0, 1.0, 1.0],
            "histogram": [0.0, 0.0, 0.0, 0.0],
        }

        state, _ = _analyze_macd_momentum(macd, "BULLISH")

        self.assertEqual(state, "NEUTRAL")

    def test_aligned_macd_is_accepted(self):
        macd = {
            "macd": [2.0, 2.0, 2.0, 2.0],
            "signal": [1.0, 1.0, 1.0, 1.0],
            "histogram": [1.0, 1.0, 1.0, 1.0],
        }

        state, _ = _analyze_macd_momentum(macd, "BULLISH")

        self.assertEqual(state, "ALIGNED")

    def test_missing_macd_is_unknown(self):
        macd = {
            "macd": [None],
            "signal": [None],
            "histogram": [None],
        }

        state, _ = _analyze_macd_momentum(macd, "BULLISH")

        self.assertEqual(state, "UNKNOWN")

    def test_opposite_macd_without_four_values_is_unclear(self):
        macd = {
            "macd": [-2.0],
            "signal": [-1.0],
            "histogram": [-1.0],
        }

        state, _ = _analyze_macd_momentum(macd, "BULLISH")

        self.assertEqual(state, "OPPOSING_UNCLEAR")

    def test_malformed_macd_is_unknown(self):
        state, _ = _analyze_macd_momentum(
            {"macd": None, "signal": [], "histogram": []},
            "BULLISH",
        )

        self.assertEqual(state, "UNKNOWN")


class TestV6M30Momentum(unittest.TestCase):

    def setUp(self):
        self.candles = make_candles(20)
        self.structure = {
            "trend": "BULLISH",
            "events": [],
            "liquidity_sweeps": [],
        }

    def test_m30_opposing_strengthening_blocks_signal(self):
        m30 = make_indicators()
        m30["macd"] = make_macd(
            [-1.0, -2.0, -3.0, -4.0],
            "BEARISH",
        )

        confirmed, reasons = _check_direction(
            "BULLISH",
            make_indicators(),
            self.structure,
            self.candles,
            m30_indicators=m30,
        )

        self.assertFalse(confirmed)
        self.assertTrue(
            any("M30" in reason and "kuchaymoqda" in reason
                for reason in reasons)
        )

    def test_m30_opposing_weakening_is_allowed(self):
        m30 = make_indicators()
        m30["macd"] = make_macd(
            [-4.0, -3.0, -2.5, -2.0],
            "BEARISH",
        )

        confirmed, reasons = _check_direction(
            "BULLISH",
            make_indicators(),
            self.structure,
            self.candles,
            m30_indicators=m30,
        )

        self.assertTrue(confirmed)
        self.assertTrue(
            any("M30" in reason and "susaymoqda" in reason
                for reason in reasons)
        )

    def test_m30_small_opposite_change_blocks_signal(self):
        m30 = make_indicators()
        m30["macd"] = make_macd(
            [-3.0, -2.0, -2.5, -2.0],
            "BEARISH",
        )

        confirmed, reasons = _check_direction(
            "BULLISH",
            make_indicators(),
            self.structure,
            self.candles,
            m30_indicators=m30,
        )

        self.assertFalse(confirmed)
        self.assertTrue(
            any("M30" in reason for reason in reasons)
        )


class TestV6StructureWindow(unittest.TestCase):

    def setUp(self):
        self.candles = make_candles(20)

    def check_event(self, event_timestamp):
        structure = {
            "trend": "UNKNOWN",
            "events": [
                SimpleNamespace(
                    timestamp=event_timestamp,
                    direction="BULLISH",
                )
            ],
            "liquidity_sweeps": [],
        }

        return _check_direction(
            "BULLISH",
            make_indicators(),
            structure,
            self.candles,
        )

    def test_bos_choch_at_oldest_of_last_12_candles_is_accepted(self):
        timestamp = self.candles[-12].timestamp

        confirmed, reasons = self.check_event(timestamp)

        self.assertTrue(confirmed)
        self.assertTrue(
            any("BOS/CHOCH" in reason for reason in reasons)
        )

    def test_bos_choch_older_than_12_candles_is_rejected(self):
        timestamp = self.candles[-13].timestamp

        confirmed, reasons = self.check_event(timestamp)

        self.assertFalse(confirmed)
        self.assertTrue(
            any("BOS/CHOCH" in reason for reason in reasons)
        )


if __name__ == "__main__":
    unittest.main()
