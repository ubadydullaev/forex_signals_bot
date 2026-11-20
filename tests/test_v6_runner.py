import unittest
from types import SimpleNamespace
from unittest.mock import patch

from v6 import runner


def make_result(
    side="BUY",
    symbol="XAUUSD",
    timeframe="M15",
    risk_reward=1.5,
):
    signal = SimpleNamespace(
        symbol=symbol,
        timeframe=timeframe,
        side=side,
        entry=2650.0,
        stop_loss=2640.0,
        take_profit=2665.0,
        risk_reward=risk_reward,
        confidence=82.5,
        reason="Test signal",
    )

    return SimpleNamespace(
        signal=signal,
        trend="BULLISH",
        strength=95.0,
    )


class TestV6Runner(unittest.TestCase):

    def test_make_signal_key(self):
        result = make_result()

        candles = {
            "M15": [
                SimpleNamespace(timestamp=123456789)
            ]
        }

        key, signature = runner.make_signal_key(
            result,
            candles,
        )

        self.assertEqual(key, "XAUUSD|M15")
        self.assertEqual(signature, "123456789|BUY")

    def test_delivery_record_new_signal(self):
        state = {"signals": {}}
        users = ["100", "200"]

        record = runner.get_delivery_record(
            state,
            "XAUUSD|M15",
            "123|BUY",
            users,
        )

        self.assertEqual(record["signature"], "123|BUY")
        self.assertEqual(record["target_users"], users)
        self.assertEqual(record["delivered_to"], [])

    def test_delivery_record_prevents_duplicates(self):
        state = {
            "signals": {
                "XAUUSD|M15": {
                    "signature": "123|BUY",
                    "delivered_to": ["100"],
                }
            }
        }

        record = runner.get_delivery_record(
            state,
            "XAUUSD|M15",
            "123|BUY",
            ["100", "200"],
        )

        self.assertEqual(record["delivered_to"], ["100"])
        self.assertEqual(record["target_users"], ["100", "200"])

    def test_new_signature_resets_delivery_list(self):
        state = {
            "signals": {
                "XAUUSD|M15": {
                    "signature": "old|BUY",
                    "delivered_to": ["100"],
                }
            }
        }

        record = runner.get_delivery_record(
            state,
            "XAUUSD|M15",
            "new|BUY",
            ["100", "200"],
        )

        self.assertEqual(record["delivered_to"], [])

    def test_format_signal_handles_missing_rr(self):
        result = make_result(risk_reward=None)

        message = runner.format_signal(result)

        self.assertIn("XAUUSD", message)
        self.assertIn("BUY", message)
        self.assertIn("Risk/Reward", message)
        self.assertIn("N/A", message)

    def test_format_signal_contains_risk_warning(self):
        message = runner.format_signal(make_result())

        self.assertIn("foyda kafolati emas", message)

    @patch.object(runner, "send_telegram")
    @patch.object(runner, "analyze_one_symbol")
    @patch.object(runner, "validate_environment")
    def test_dry_run_does_not_send_telegram(
        self,
        mock_validate,
        mock_analyze,
        mock_send,
    ):
        result = make_result()

        candles = {
            "M15": [
                SimpleNamespace(timestamp=123456789)
            ]
        }

        mock_analyze.return_value = (result, candles)

        with patch.object(runner, "SYMBOLS", ("XAUUSD",)):
            with patch.object(runner, "TELEGRAM_ENABLED", False):
                stats = runner.main()

        mock_send.assert_not_called()
        self.assertEqual(stats["analyzed"], 1)
        self.assertEqual(stats["buy"], 1)
        self.assertEqual(stats["sent"], 0)

    @patch.object(runner, "session")
    def test_get_users_deduplicates_and_sorts(self, mock_session):
        response = SimpleNamespace(
            json=lambda: {"users": ["200", "100", "200"]},
            raise_for_status=lambda: None,
        )

        mock_session.get.return_value = response

        with patch.object(runner, "WORKER_URL", "https://worker.example"):
            with patch.object(runner, "WEBHOOK_SECRET", "secret"):
                with patch.object(runner, "ADMIN_ID", "300"):
                    users = runner.get_users()

        self.assertEqual(users, ["100", "200", "300"])


if __name__ == "__main__":
    unittest.main()
