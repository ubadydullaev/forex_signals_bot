
import csv
import os

from .cost_model import (
    COST_SCENARIOS,
    calculate_cost_r,
    calculate_scenario_results,
)

INPUT_FILE = "backtest_results/v6_trades.csv"
OUTPUT_FILE = "backtest_results/v6_cost_results.csv"


def main():
    if not os.path.exists(INPUT_FILE):
        raise FileNotFoundError(
            f"Backtest fayli topilmadi: {INPUT_FILE}"
        )

    with open(INPUT_FILE, "r", newline="", encoding="utf-8") as file:
        trades = list(csv.DictReader(file))

    if not trades:
        print("Savdolar topilmadi.")
        return

    gross_values = [
        float(trade["r_multiple"])
        for trade in trades
    ]

    results = calculate_scenario_results(gross_values)

    fields = [
        "symbol",
        "side",
        "entry_time",
        "exit_time",
        "outcome",
        "gross_r",
        "base_cost_r",
        "base_net_r",
        "high_cost_r",
        "high_net_r",
    ]

    os.makedirs("backtest_results", exist_ok=True)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()

        for trade in trades:
            gross = float(trade["r_multiple"])

            writer.writerow({
                "symbol": trade["symbol"],
                "side": trade["side"],
                "entry_time": trade["entry_time"],
                "exit_time": trade["exit_time"],
                "outcome": trade["outcome"],
                "gross_r": round(gross, 4),
                "base_cost_r": calculate_cost_r("BASE_STRESS"),
                "base_net_r": round(
                    gross - calculate_cost_r("BASE_STRESS"), 4
                ),
                "high_cost_r": calculate_cost_r("HIGH_STRESS"),
                "high_net_r": round(
                    gross - calculate_cost_r("HIGH_STRESS"), 4
                ),
            })

    print("\n===== V6 XARAJATLI BACKTEST =====")
    print("Savdolar:", len(trades))
    print("Xarajatlar: spread + slippage stress-taxminlari")
    print("Komissiya: 0R deb faraz qilingan")
    print("Bu Libertexning tasdiqlangan kotirovkasi emas.\n")

    for name, data in results.items():
        print(f"[{name}]")
        print("Xarajat / savdo:", f"{data['cost_per_trade_r']:.3f}R")
        print("Sof natija:", f"{data['total_net_r']:.2f}R")
        print("O'rtacha:", f"{data['average_net_r']:.3f}R")
        print("Sof win rate:", f"{data['win_rate']:.2f}%")
        print("Profit factor:", f"{data['profit_factor']:.3f}")
        print("Max drawdown:", f"{data['max_drawdown_r']:.2f}R")
        print()

    print("CSV:", OUTPUT_FILE)


if __name__ == "__main__":
    main()
  
