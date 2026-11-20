
"""
V6 transaction-cost stress model.

R qiymatlari broker kotirovkasi emas:
ular spread va slippage ta'sirini tekshirish
uchun alohida stress-taxminlardir.
"""

COST_SCENARIOS = {
    "NO_COST": {
        "spread_r": 0.00,
        "slippage_r": 0.00,
        "commission_r": 0.00,
    },
    "BASE_STRESS": {
        "spread_r": 0.04,
        "slippage_r": 0.01,
        "commission_r": 0.00,
    },
    "HIGH_STRESS": {
        "spread_r": 0.08,
        "slippage_r": 0.02,
        "commission_r": 0.00,
    },
}


def calculate_cost_r(scenario_name):
    if scenario_name not in COST_SCENARIOS:
        raise ValueError(
            f"Noma'lum xarajat ssenariysi: {scenario_name}"
        )

    costs = COST_SCENARIOS[scenario_name]

    return sum(costs.values())


def calculate_net_r(gross_r, scenario_name):
    return gross_r - calculate_cost_r(scenario_name)


def calculate_scenario_results(gross_r_values):
    results = {}

    for scenario_name in COST_SCENARIOS:
        cost_r = calculate_cost_r(scenario_name)

        net_values = [
            value - cost_r
            for value in gross_r_values
        ]

        wins = sum(value > 0 for value in net_values)
        losses = sum(value < 0 for value in net_values)

        gross_profit = sum(
            value for value in net_values if value > 0
        )
        gross_loss = abs(sum(
            value for value in net_values if value < 0
        ))

        profit_factor = (
            gross_profit / gross_loss
            if gross_loss > 0
            else float("inf") if gross_profit > 0 else 0.0
        )

        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0

        for value in net_values:
            equity += value
            peak = max(peak, equity)
            max_drawdown = min(
                max_drawdown,
                equity - peak,
            )

        count = len(net_values)

        results[scenario_name] = {
            "trades": count,
            "cost_per_trade_r": cost_r,
            "total_net_r": sum(net_values),
            "average_net_r": (
                sum(net_values) / count if count else 0.0
            ),
            "win_rate": (
                wins / count * 100 if count else 0.0
            ),
            "profit_factor": profit_factor,
            "max_drawdown_r": max_drawdown,
            "wins": wins,
            "losses": losses,
        }

    return results
