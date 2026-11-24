
import unittest
from types import SimpleNamespace

from v6.runner import (
    format_public_signal,
    get_public_entry_zone,
)


class TestV6PublicSignalFormat(unittest.TestCase):

    def make_result(self):
        signal = SimpleNamespace(
            symbol="XAUUSD",
            side="BUY",
            entry=2650.0,
            stop_loss=2640.0,
            take_profit=2665.0,
        )
        return SimpleNamespace(signal=signal)

    def test_public_message_contains_entry_zone_sl_and_tp_only(self):
        result = self.make_result()

        candles = {
            "M15": [
                SimpleNamespace(open=2648.0, close=2650.0)
            ]
        }

        message = format_public_signal(result, candles)

        self.assertIn("XAUUSD", message)
        self.assertIn("BUY", message)
        self.assertIn("2648.000", message)
        self.assertIn("2650.000", message)
        self.assertIn("2640.000", message)
        self.assertIn("2665.000", message)

        self.assertNotIn("Risk/Reward", message)
        self.assertNotIn("ADX", message)
        self.assertNotIn("Tasdiqlar", message)
        self.assertNotIn("Texnik baho", message)

    def test_missing_open_close_uses_signal_entry(self):
        result = self.make_result()

        candles = {
            "M15": [
                SimpleNamespace(timestamp=123456789)
            ]
        }

        zone = get_public_entry_zone(result, candles)

        self.assertEqual(zone, "2650.000 – 2650.000")

    def test_empty_candles_use_signal_entry(self):
        result = self.make_result()

        zone = get_public_entry_zone(result, {"M15": []})

        self.assertEqual(zone, "2650.000 – 2650.000")

    def test_missing_candles_use_signal_entry(self):
        result = self.make_result()

        zone = get_public_entry_zone(result)

        self.assertEqual(zone, "2650.000 – 2650.000")


if __name__ == "__main__":
    unittest.main()
