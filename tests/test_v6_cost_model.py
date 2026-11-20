import unittest

from v6.cost_model import (
    COST_SCENARIOS,
    calculate_trade_cost_r,
    calculate_results,
    calculate_scenario_results,
    calculate_break_even_price_cost,
)


class TestV6CostModel(unittest.TestCase):

    def test_scenarios_are_defined(self):
        self.assertEqual(COST_SCENARIOS["NO_COST"], 0.0)
        self.assertEqual(COST_SCENARIOS["BASE_STRESS"], 0.05)
        self.assertEqual(COST_SCENARIOS["HIGH_STRESS"], 0.10)

    def test_trade_cost_calculation(self):
        result = calculate_trade_cost_r(
            0.10, 0.05, 0.02, 1.50
        )
        self.assertAlmostEqual(result, 0.12)

    def test_negative_cost_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_trade_cost_r(
                -0.1, 0.0, 0.0, 1.0
            )

    def test_zero_risk_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_trade_cost_r(
                0.1, 0.0, 0.0, 0.0
            )

    def test_results_calculation(self):
        result = calculate_results(
            [1.0, -0.5, 1.0],
            [0.1, 0.1, 0.1],
        )

        self.assertEqual(result["trades"], 3)
        self.assertAlmostEqual(result["total_net_r"], 1.2)
        self.assertAlmostEqual(result["average_net_r"], 0.4)
        self.assertAlmostEqual(result["win_rate"], 66.6666667)

    def test_mismatched_trade_and_cost_counts(self):
        with self.assertRaises(ValueError):
            calculate_results([1.0, -1.0], [0.1])

    def test_scenario_results(self):
        results = calculate_scenario_results([1.0, -0.5])

        self.assertIn("NO_COST", results)
        self.assertIn("BASE_STRESS", results)
        self.assertIn("HIGH_STRESS", results)

        self.assertAlmostEqual(
            results["BASE_STRESS"]["total_net_r"],
            0.4,
        )

    def test_break_even_cost(self):
        result = calculate_break_even_price_cost(
            [1.0, -0.5],
            [10.0, 10.0],
        )

        self.assertAlmostEqual(result, 2.5)

    def test_break_even_without_profit(self):
        result = calculate_break_even_price_cost(
            [-1.0, -0.5],
            [10.0, 10.0],
        )

        self.assertIsNone(result)

    def test_empty_break_even(self):
        result = calculate_break_even_price_cost([], [])
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
