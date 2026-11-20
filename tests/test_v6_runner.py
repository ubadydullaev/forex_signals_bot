
import base64
import json
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
            result, candles
        )

        self.assertEqual(key, "XAUUSD|M15")
        self.assertEqual(signature, "123456789|BUY")

    def test_delivery_record_new_signal(self):
        state = {"signals": {}}
        users = ["100", "200"]

        record = runner.get_delivery_record(
            state, "XAUUSD|M15", "123|BUY", users
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
        message = runner.format_signal(
            make_result(risk_reward=None)
        )

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
        self, mock_validate, mock_analyze, mock_send
    ):
        mock_analyze.return_value = (
            make_result(),
            {
                "M15": [
                    SimpleNamespace(timestamp=123456789)
                ]
            },
        )

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

    @patch.object(runner, "session")
    def test_load_state_decodes_github_content(self, mock_session):
        original = {
            "signals": {
                "XAUUSD|M15": {
                    "signature": "123|BUY",
                    "delivered_to": ["100"],
                }
            }
        }

        encoded = base64.b64encode(
            json.dumps(original).encode("utf-8")
        ).decode("utf-8")

        mock_session.get.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "content": encoded,
                "sha": "old-sha",
            },
            raise_for_status=lambda: None,
        )

        with patch.object(runner, "GITHUB_TOKEN", "token"):
            with patch.object(runner, "GITHUB_REPOSITORY", "owner/repo"):
                state, sha = runner.load_state()

        self.assertEqual(state, original)
        self.assertEqual(sha, "old-sha")

    @patch.object(runner, "session")
    def test_load_state_returns_empty_on_404(self, mock_session):
        mock_session.get.return_value = SimpleNamespace(
            status_code=404
        )

        with patch.object(runner, "GITHUB_TOKEN", "token"):
            with patch.object(runner, "GITHUB_REPOSITORY", "owner/repo"):
                state, sha = runner.load_state()

        self.assertEqual(state, {"signals": {}})
        self.assertIsNone(sha)

    @patch.object(runner, "session")
    def test_save_state_encodes_and_returns_new_sha(self, mock_session):
        mock_session.put.return_value = SimpleNamespace(
            json=lambda: {
                "content": {"sha": "new-sha"}
            },
            raise_for_status=lambda: None,
        )

        state = {
            "signals": {
                "XAUUSD|M15": {
                    "signature": "123|BUY",
                    "delivered_to": ["100"],
                }
            }
        }

        with patch.object(runner, "GITHUB_TOKEN", "token"):
            with patch.object(runner, "GITHUB_REPOSITORY", "owner/repo"):
                new_sha = runner.save_state(state, "old-sha")

        self.assertEqual(new_sha, "new-sha")

        payload = mock_session.put.call_args.kwargs["json"]
        decoded = json.loads(
            base64.b64decode(payload["content"]).decode("utf-8")
        )

        self.assertEqual(decoded, state)
        self.assertEqual(payload["sha"], "old-sha")

    @patch.object(runner, "session")
    def test_send_telegram_success(self, mock_session):
        mock_session.post.return_value = SimpleNamespace(
            json=lambda: {"ok": True},
            raise_for_status=lambda: None,
        )

        with patch.object(runner, "TELEGRAM_ENABLED", True):
            with patch.object(runner, "BOT_TOKEN", "test-token"):
                sent = runner.send_telegram("123", "Test message")

        self.assertTrue(sent)
        mock_session.post.assert_called_once()

        payload = mock_session.post.call_args.kwargs["json"]
        self.assertEqual(payload["chat_id"], "123")
        self.assertEqual(payload["text"], "Test message")
        self.assertEqual(payload["parse_mode"], "HTML")

    @patch.object(runner, "session")
    def test_send_telegram_rejects_api_failure(self, mock_session):
        mock_session.post.return_value = SimpleNamespace(
            json=lambda: {
                "ok": False,
                "description": "Chat not found",
            },
            raise_for_status=lambda: None,
        )

        with patch.object(runner, "TELEGRAM_ENABLED", True):
            with patch.object(runner, "BOT_TOKEN", "test-token"):
                with self.assertRaises(RuntimeError):
                    runner.send_telegram("123", "Test message")


if __name__ == "__main__":
    unittest.main()
