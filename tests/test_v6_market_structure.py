
import unittest

from v6.models import Candle
from v6.market_structure import (
    SwingPoint,
    find_swing_points,
    detect_trend,
    detect_structure_events,
    detect_liquidity_sweeps,
    get_support_resistance,
    analyze_market_structure,
)


class TestV6MarketStructure(unittest.TestCase):

    def make_candles(self, values):
        candles = []

        for i, (high, low, close) in enumerate(values):
            candles.append(
                Candle(
                    timestamp=1760000000000 + i * 60000,
                    open=float(close),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=100.0,
                    is_closed=True,
                    source="test",
                )
            )

        return candles

    # SWING POINT TESTS

    def test_swing_high_detection(self):
        candles = self.make_candles([
            (10, 5, 8),
            (12, 6, 9),
            (15, 7, 12),
            (13, 6, 10),
            (11, 5, 8),
        ])

        swings = find_swing_points(
            candles,
            left=2,
            right=2,
        )

        highs = [
            point for point in swings
            if point.kind == "HIGH"
        ]

        self.assertEqual(len(highs), 1)
        self.assertEqual(highs[0].index, 2)
        self.assertEqual(highs[0].price, 15.0)

    def test_swing_low_detection(self):
        candles = self.make_candles([
            (15, 10, 12),
            (14, 8, 10),
            (13, 4, 7),
            (14, 8, 10),
            (15, 9, 12),
        ])

        swings = find_swing_points(
            candles,
            left=2,
            right=2,
        )

        lows = [
            point for point in swings
            if point.kind == "LOW"
        ]

        self.assertEqual(len(lows), 1)
        self.assertEqual(lows[0].index, 2)
        self.assertEqual(lows[0].price, 4.0)

    def test_unconfirmed_final_swing_is_not_used(self):
        candles = self.make_candles([
            (10, 5, 8),
            (11, 6, 9),
            (20, 4, 12),
            (12, 6, 9),
        ])

        swings = find_swing_points(
            candles,
            left=2,
            right=2,
        )

        self.assertEqual(swings, [])

    def test_swing_rejects_invalid_parameters(self):
        candles = self.make_candles([
            (10, 5, 8),
            (11, 6, 9),
        ])

        with self.assertRaises(ValueError):
            find_swing_points(candles, left=0, right=2)

        with self.assertRaises(ValueError):
            find_swing_points(candles, left=2, right=0)

        with self.assertRaises(ValueError):
            find_swing_points(candles, left=True, right=2)

    # TREND TESTS

    def test_bullish_trend_detection(self):
        swings = [
            SwingPoint(1, 1, 10, "HIGH"),
            SwingPoint(2, 2, 5, "LOW"),
            SwingPoint(3, 3, 12, "HIGH"),
            SwingPoint(4, 4, 7, "LOW"),
        ]

        self.assertEqual(detect_trend(swings), "BULLISH")

    def test_bearish_trend_detection(self):
        swings = [
            SwingPoint(1, 1, 15, "HIGH"),
            SwingPoint(2, 2, 10, "LOW"),
            SwingPoint(3, 3, 12, "HIGH"),
            SwingPoint(4, 4, 7, "LOW"),
        ]

        self.assertEqual(detect_trend(swings), "BEARISH")

    def test_ranging_trend_detection(self):
        swings = [
            SwingPoint(1, 1, 10, "HIGH"),
            SwingPoint(2, 2, 5, "LOW"),
            SwingPoint(3, 3, 12, "HIGH"),
            SwingPoint(4, 4, 4, "LOW"),
        ]

        self.assertEqual(detect_trend(swings), "RANGING")

    def test_unknown_trend_with_insufficient_swings(self):
        swings = [
            SwingPoint(1, 1, 10, "HIGH"),
            SwingPoint(2, 2, 5, "LOW"),
        ]

        self.assertEqual(detect_trend(swings), "UNKNOWN")

    # BOS AND CHOCH TESTS

    def test_bullish_bos_detection(self):
        candles = self.make_candles([
            (9, 6, 8),
            (10, 6, 9),
            (9, 5, 8),
            (12, 7, 11),
        ])

        swings = [
            SwingPoint(1, candles[1].timestamp, 10, "HIGH"),
        ]

        events = detect_structure_events(
            candles,
            swings,
            right=1,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "BOS")
        self.assertEqual(events[0].direction, "BULLISH")
        self.assertEqual(events[0].level, 10)

    def test_bearish_bos_detection(self):
        candles = self.make_candles([
            (12, 8, 10),
            (11, 7, 9),
            (12, 8, 10),
            (9, 5, 6),
        ])

        swings = [
            SwingPoint(1, candles[1].timestamp, 7, "LOW"),
        ]

        events = detect_structure_events(
            candles,
            swings,
            right=1,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "BOS")
        self.assertEqual(events[0].direction, "BEARISH")
        self.assertEqual(events[0].level, 7)

    def test_bearish_choch_after_bullish_structure(self):
        candles = self.make_candles([
            (9, 7, 8),
            (11, 8, 9),
            (9, 6, 8),
            (12, 8, 11),
            (8, 5, 6),
        ])

        swings = [
            SwingPoint(1, candles[1].timestamp, 10, "HIGH"),
            SwingPoint(2, candles[2].timestamp, 7, "LOW"),
        ]

        events = detect_structure_events(
            candles,
            swings,
            right=1,
        )

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event, "BOS")
        self.assertEqual(events[0].direction, "BULLISH")
        self.assertEqual(events[1].event, "CHOCH")
        self.assertEqual(events[1].direction, "BEARISH")

    def test_swing_cannot_break_before_confirmation(self):
        candles = self.make_candles([
            (9, 6, 8),
            (12, 6, 11),
            (9, 5, 8),
            (13, 7, 12),
        ])

        swings = [
            SwingPoint(1, candles[1].timestamp, 10, "HIGH"),
        ]

        events = detect_structure_events(
            candles,
            swings,
            right=2,
        )

        self.assertEqual(events, [])

    def test_no_late_bos_if_level_was_already_crossed(self):
        candles = self.make_candles([
            (9, 6, 8),
            (12, 7, 11),
            (13, 8, 12),
            (14, 9, 13),
            (15, 10, 14),
        ])

        swings = [
            SwingPoint(1, candles[1].timestamp, 10, "HIGH"),
        ]

        events = detect_structure_events(
            candles,
            swings,
            right=2,
        )

        self.assertEqual(events, [])

    # LIQUIDITY SWEEP TESTS

    def test_bearish_liquidity_sweep_detection(self):
        candles = self.make_candles([
            (10, 6, 8),
            (9, 5, 7),
            (12, 7, 9),
        ])

        swings = [
            SwingPoint(0, candles[0].timestamp, 10, "HIGH"),
        ]

        sweeps = detect_liquidity_sweeps(
            candles,
            swings,
            right=1,
        )

        self.assertEqual(len(sweeps), 1)
        self.assertEqual(sweeps[0].direction, "BEARISH")
        self.assertEqual(sweeps[0].swept_level, 10)

    def test_bullish_liquidity_sweep_detection(self):
        candles = self.make_candles([
            (12, 8, 10),
            (11, 7, 9),
            (10, 5, 9),
        ])

        swings = [
            SwingPoint(0, candles[0].timestamp, 8, "LOW"),
        ]

        sweeps = detect_liquidity_sweeps(
            candles,
            swings,
            right=1,
        )

        self.assertEqual(len(sweeps), 1)
        self.assertEqual(sweeps[0].direction, "BULLISH")
        self.assertEqual(sweeps[0].swept_level, 8)

    def test_same_liquidity_level_is_not_repeatedly_counted(self):
        candles = self.make_candles([
            (10, 6, 8),
            (9, 5, 7),
            (12, 7, 9),
            (13, 7, 9),
            (12, 6, 8),
        ])

        swings = [
            SwingPoint(0, candles[0].timestamp, 10, "HIGH"),
        ]

        sweeps = detect_liquidity_sweeps(
            candles,
            swings,
            right=1,
        )

        self.assertEqual(len(sweeps), 1)

    def test_broken_high_is_not_later_counted_as_sweep(self):
        candles = self.make_candles([
            (10, 6, 8),
            (9, 5, 7),
            (12, 8, 11),
            (13, 7, 9),
        ])

        swings = [
            SwingPoint(0, candles[0].timestamp, 10, "HIGH"),
        ]

        sweeps = detect_liquidity_sweeps(
            candles,
            swings,
            right=1,
        )

        self.assertEqual(sweeps, [])

    # SUPPORT AND RESISTANCE TESTS

    def test_support_and_resistance(self):
        candles = self.make_candles([
            (12, 7, 10),
        ])

        swings = [
            SwingPoint(1, 1, 90, "LOW"),
            SwingPoint(2, 2, 110, "HIGH"),
            SwingPoint(3, 3, 95, "LOW"),
            SwingPoint(4, 4, 120, "HIGH"),
        ]

        levels = get_support_resistance(
            candles,
            swings,
            current_price=100,
        )

        self.assertEqual(levels["support"], 95)
        self.assertEqual(levels["resistance"], 110)

    def test_no_support_or_resistance(self):
        candles = self.make_candles([
            (12, 7, 10),
        ])

        levels = get_support_resistance(
            candles,
            [],
            current_price=10,
        )

        self.assertIsNone(levels["support"])
        self.assertIsNone(levels["resistance"])

    # COMPLETE ANALYSIS TESTS

    def test_empty_market_structure(self):
        result = analyze_market_structure([])

        self.assertEqual(result["trend"], "UNKNOWN")
        self.assertEqual(result["swings"], [])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["liquidity_sweeps"], [])
        self.assertIsNone(result["levels"]["support"])
        self.assertIsNone(result["levels"]["resistance"])

    def test_complete_analysis_result_keys(self):
        candles = self.make_candles([
            (10, 5, 8),
            (15, 7, 12),
            (12, 6, 9),
            (11, 5, 8),
            (14, 7, 11),
            (12, 6, 9),
            (10, 4, 7),
        ])

        result = analyze_market_structure(
            candles,
            left=1,
            right=1,
        )

        self.assertEqual(
            set(result.keys()),
            {
                "trend",
                "swings",
                "events",
                "liquidity_sweeps",
                "levels",
            },
        )

        self.assertIn(
            result["trend"],
            ("UNKNOWN", "BULLISH", "BEARISH", "RANGING"),
        )


if __name__ == "__main__":
    unittest.main()
        
