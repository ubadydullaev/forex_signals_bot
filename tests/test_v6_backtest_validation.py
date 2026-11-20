
import unittest

from v6.backtest import validate_cached_trade


class TestV6BacktestValidation(unittest.TestCase):

    def test_valid_trade_is_accepted(self):
        valid, reason = validate_cached_trade(
            "BUY", 100.0, 98.5, 102.25, 1.0
        )
        self.assertTrue(valid, reason)

    def test_stop_loss_too_tight_is_rejected(self):
        valid, reason = validate_cached_trade(
            "BUY", 100.0, 99.8, 100.3, 1.0
        )
        self.assertFalse(valid)
        self.assertIn("Stop-loss too tight", reason)

    def test_stop_loss_too_wide_is_rejected(self):
        valid, reason = validate_cached_trade(
            "BUY", 100.0, 96.0, 106.0, 1.0
        )
        self.assertFalse(valid)
        self.assertIn("Stop-loss too wide", reason)

    def test_wrong_stop_loss_side_is_rejected(self):
        valid, reason = validate_cached_trade(
            "BUY", 100.0, 101.0, 103.0, 1.0
        )
        self.assertFalse(valid)

    def test_insufficient_rr_is_rejected(self):
        valid, reason = validate_cached_trade(
            "BUY", 100.0, 98.5, 101.0, 1.0
        )
        self.assertFalse(valid)
        self.assertIn("Risk/reward too low", reason)

    def test_invalid_atr_is_rejected(self):
        valid, reason = validate_cached_trade(
            "BUY", 100.0, 98.5, 102.25, 0.0
        )
        self.assertFalse(valid)


if __name__ == "__main__":
    unittest.main()
      
