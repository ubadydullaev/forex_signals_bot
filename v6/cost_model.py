import math

COST_SCENARIOS = {
    "NO_COST": 0.00,
    "BASE_STRESS": 0.05,
    "HIGH_STRESS": 0.10,
}


def calculate_cost_r(scenario_name):
    if scenario_name not in COST_SCENARIOS:
        raise ValueError(
            f"Noma'lum ssenariy: {scenario_name}"
        )
    return COST_SCENARIOS[scenario_name]


def calculate_trade_cost_r(
    spread_price,
    slippage_price,
    commission_r,
    risk_distance,
):
    values = (
        spread_price,
        slippage_price,
        commission_r,
        risk_distance,
    )

    if not all(math.isfinite(float(v)) for v in values):
        raise ValueError("Xarajat qiymati noto'g'ri.")

    if min(spread_price, slippage_price, commission_r) < 0:
        raise ValueError("Xarajat manfiy bo'lishi mumkin emas.")

    if risk_distance <= 0:
        raise ValueError("Risk masofasi musbat bo'lishi kerak.")

    return (
        (spread_price + slippage_price) / risk_distance
        + commission_r
    )


def calculate_results(gross_values, cost_values):
    if len(gross_values) != len(cost_values):
        raise ValueError("Savdo va xarajatlar soni mos emas.")

    net_values = [
        gross - cost
        for gross, cost in zip(gross_values, cost_values)
    ]

    count = len(net_values)
    wins = sum(value > 0 for value in net_values)
    losses = sum(value < 0 for value in net_values)

    gross_profit = sum(
        value for value in net_values if value > 0
    )
    gross_loss = abs(sum(
        value for value in net_values if value < 0
    ))

    if gross_loss:
        pf = gross_profit / gross_loss
    else:
        pf = float("inf") if gross_profit else 0.0

    equity = 0.0
    peak = 0.0
    max_dd = 0.0

    for value in net_values:
        equity += value
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    return {
        "trades": count,
        "total_net_r": sum(net_values),
        "average_net_r": (
            sum(net_values) / count if count else 0.0
        ),
        "win_rate": (
            wins / count * 100 if count else 0.0
        ),
        "profit_factor": pf,
        "max_drawdown_r": max_dd,
        "average_cost_r": (
            sum(cost_values) / count if count else 0.0
        ),
        "wins": wins,
        "losses": losses,
    }


def calculate_scenario_results(gross_values):
    results = {}

    for name, cost in COST_SCENARIOS.items():
        results[name] = calculate_results(
            gross_values,
            [cost] * len(gross_values),
        )
        results[name]["cost_per_trade_r"] = cost

    return results


def calculate_break_even_price_cost(
    gross_values,
    risk_distances,
):
    if len(gross_values) != len(risk_distances):
        raise ValueError("Savdo va risk soni mos emas.")

    if not gross_values:
        return None

    if any(
        not math.isfinite(float(risk)) or risk <= 0
        for risk in risk_distances
    ):
        raise ValueError("Risk masofasi noto'g'ri.")

    total_gross = sum(gross_values)

    if total_gross <= 0:
        return None

    denominator = sum(
        1 / risk for risk in risk_distances
    )

    if denominator <= 0:
        return None

    return total_gross / denominator
