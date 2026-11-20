
import csv
import os
from collections import defaultdict

from .cost_model import (
    COST_SCENARIOS,
    calculate_cost_r,
    calculate_scenario_results,
)

INPUT_FILE = "backtest_results/v6_trades.csv"
TRADE_OUTPUT = "backtest_results/v6_cost_results.csv"
SUMMARY_OUTPUT = "backtest_results/v6_cost_summary.csv"


def main():
    if not os.path.exists(INPUT_FILE):
        raise FileNotFoundError(
            f"Backtest fayli topilmadi: {INPUT_FILE}"
        )

    with open(
        INPUT_FILE, "r", newline="", encoding="utf-8"
    ) as file:
        trades = list(csv.DictReader(file))

    if not trades:
        raise RuntimeError("Savdolar topilmadi.")

    grouped = defaultdict(list)

    for trade in trades:
        symbol = trade.get("symbol", "").strip()
        if not symbol:
            raise ValueError("Savdoda symbol qiymati yo'q.")

        gross = float(trade["r_multiple"])
        grouped[symbol].append((trade, gross))

    os.makedirs("backtest_results", exist_ok=True)

    trade_fields = [
        "symbol", "side", "entry_time", "exit_time",
        "outcome", "gross_r",
        "base_cost_r", "base_net_r",
        "high_cost_r", "high_net_r",
    ]

    with open(
        TRADE_OUTPUT, "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(file, fieldnames=trade_fields)
        writer.writeheader()

        for symbol, items in sorted(grouped.items()):
            for trade, gross in items:
                writer.writerow({
                    "symbol": symbol,
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

    summary_fields = [
        "symbol", "scenario", "trades",
        "cost_per_trade_r", "total_net_r",
        "average_net_r", "win_rate",
        "profit_factor", "max_drawdown_r",
    ]

    with open(
        SUMMARY_OUTPUT, "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(file, fieldnames=summary_fields)
        writer.writeheader()

        for symbol, items in sorted(grouped.items()):
            values = [gross for _, gross in items]
            results = calculate_scenario_results(values)

            for scenario, data in results.items():
                writer.writerow({
                    "symbol": symbol,
                    "scenario": scenario,
                    "trades": data["trades"],
                    "cost_per_trade_r": data["cost_per_trade_r"],
                    "total_net_r": round(data["total_net_r"], 4),
                    "average_net_r": round(data["average_net_r"], 4),
                    "win_rate": round(data["win_rate"], 2),
                    "profit_factor": round(data["profit_factor"], 4),
                    "max_drawdown_r": round(
                        data["max_drawdown_r"], 4
                    ),
                })

        all_values = [
            gross
            for items in grouped.values()
            for _, gross in items
        ]
        overall = calculate_scenario_results(all_values)

        for scenario, data in overall.items():
            writer.writerow({
                "symbol": "ALL",
                "scenario": scenario,
                "trades": data["trades"],
                "cost_per_trade_r": data["cost_per_trade_r"],
                "total_net_r": round(data["total_net_r"], 4),
                "average_net_r": round(data["average_net_r"], 4),
                "win_rate": round(data["win_rate"], 2),
                "profit_factor": round(data["profit_factor"], 4),
                "max_drawdown_r": round(
                    data["max_drawdown_r"], 4
                ),
            })

    print("\n===== V6 INSTRUMENT BO'YICHA XARAJAT TAHLILI =====")
    print("Jami savdolar:", len(trades))
    print("Instrumentlar:", len(grouped))
    print("Eslatma: xarajatlar hozircha umumiy stress-taxminlar.")
    print("Libertexning real spredi sifatida qabul qilinmasin.\n")

    for symbol, items in sorted(grouped.items()):
        print(f"\n[{symbol}] Savdolar: {len(items)}")

        results = calculate_scenario_results(
            [gross for _, gross in items]
        )

        for scenario, data in results.items():
            print(
                f"{scenario}: "
                f"Net={data['total_net_r']:.2f}R | "
                f"PF={data['profit_factor']:.3f} | "
                f"WR={data['win_rate']:.2f}% | "
                f"DD={data['max_drawdown_r']:.2f}R"
            )

    print("\nUmumiy natijalar:")
    for scenario, data in overall.items():
        print(
            f"{scenario}: "
            f"Net={data['total_net_r']:.2f}R | "
            f"PF={data['profit_factor']:.3f}"
        )

    print("\nHisobotlar:")
    print(TRADE_OUTPUT)
    print(SUMMARY_OUTPUT)


if __name__ == "__main__":
    main()
