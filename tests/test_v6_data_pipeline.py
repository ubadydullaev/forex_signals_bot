import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from v6 import data_adapter
from v6.data_pipeline import get_verified_market_data
from v6.models import Candle


class TestV6DataPipeline(unittest.TestCase):

    def make_candles(
        self,
        count,
        timeframe,
        source,
        gap_after=None,
    ):
        duration = data_adapter.TIMEFRAME_SETTINGS[timeframe]["duration_ms"]
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        latest_open = (now_ms // duration) * duration - 2 * duration

        candles = []

        for index in range(count):
            timestamp = (
                latest_open - (count - 1 - index) * duration
            )

            if gap_after is not None and index > gap_after:
                timestamp += duration

            price = 50000.0 if source == "binance_spot" else 1.10000

            candles.append(
                Candle(
                    timestamp=timestamp,
                    open=price,
                    high=price + 10,
                    low=price - 10,
                    close=price + 2,
                    volume=100,
                    is_closed=True,
                    source=source,
                )
            )

        return candles

    def test_short_lse_history_falls_back_to_forexsb(self):
        lse_candles = self.make_candles(
            250, "M15", "lse"
        )
        forexsb_candles = self.make_candles(
            1000, "M15", "forexsb"
        )

        with patch.object(
            data_adapter,
            "_fetch_lse",
            return_value=lse_candles,
        ) as mock_lse:
            with patch.object(
                data_adapter,
                "_fetch_forexsb",
                return_value=forexsb_candles,
            ) as mock_forexsb:
                result = get_verified_market_data(
                    "EURUSD",
                    "M15",
                    1000,
                )

        self.assertEqual(len(result.candles), 1000)
        self.assertEqual(result.candles[-1].source, "forexsb")
        mock_lse.assert_called_once()
        mock_forexsb.assert_called_once()

    def test_binance_gap_is_rejected(self):
        candles = self.make_candles(
            1000,
            "M15",
            "binance_spot",
            gap_after=500,
        )

        with patch.object(
            data_adapter,
            "_fetch_binance",
            return_value=candles,
        ):
            with self.assertRaises(RuntimeError):
                get_verified_market_data(
                    "BTCUSD",
                    "M15",
                    1000,
                )

    def test_valid_binance_history_passes(self):
        candles = self.make_candles(
            1000,
            "M15",
            "binance_spot",
        )

        with patch.object(
            data_adapter,
            "_fetch_binance",
            return_value=candles,
        ):
            result = get_verified_market_data(
                "BTCUSD",
                "M15",
                1000,
            )

        self.assertEqual(len(result.candles), 1000)
        self.assertEqual(result.candles[-1].source, "binance_spot")

    def test_insufficient_history_is_rejected(self):
        candles = self.make_candles(
            250,
            "M15",
            "binance_spot",
        )

        with patch.object(
            data_adapter,
            "_fetch_binance",
            return_value=candles,
        ):
            with self.assertRaises(RuntimeError):
                get_verified_market_data(
                    "BTCUSD",
                    "M15",
                    1000,
                )


if __name__ == "__main__":
    unittest.main()
