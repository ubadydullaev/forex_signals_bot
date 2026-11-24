
import unittest
from types import SimpleNamespace

from v6.runner import format_public_signal


class TestV6PublicSignalFormat(unittest.TestCase):

    def test_public_message_contains_entry_zone_sl_and_tp_only(self):
        signal = SimpleNamespace(
            symbol="XAUUSD",
            side="BUY",
            entry=2650.0,
            stop_loss=2640.0,
            take_profit=2665.0,
        )

        result = SimpleNamespace(signal=signal)

        candles = {
            "M15": [
                SimpleNamespace(
                    open=2648.0,
                    close=2650.0,
                )
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


if __name__ == "__main__":
    unittest.main()
