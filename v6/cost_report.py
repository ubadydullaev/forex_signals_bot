import csv
import math
import os
from collections import defaultdict

from .cost_model import (
    COST_SCENARIOS,
    calculate_trade_cost_r,
    calculate_results,
    calculate_break_even_price_cost,
)

INPUT_FILE = "backtest_results/v6_trades.csv"
BROKER_FILE = "v6/broker_costs.csv"

TRADE_OUTPUT = "backtest_results/v6_cost_results.csv"
SUMMARY_OUTPUT = "backtest_results/v6_cost_summary.csv"



def load_broker_costs():
    if not os.path.exists(BROKER_FILE):
        return {}

    costs = {}

    with open(
        BROKER_FILE, "r", newline="", encoding="utf-8"
    ) as file:
        for row in csv.DictReader(file):
            symbol = row.get("symbol", "").strip()

            if not symbol:
                continue

            raw = [
                row.get("spread_price", "").strip(),
                row.get("slippage_price", "").strip(),
                row.get("commission_r", "").strip(),
            ]

            if not any(raw):
                continue

            try:
                spread = float(raw[0]) if raw[0] else 0.0
                slippage = float(raw[1]) if raw[1] else 0.0
                commission = float(raw[2]) if raw[2] else 0.0
            except ValueError as error:
                raise ValueError(
                    f"{symbol}: xarajat qiymati raqam bo'lishi kerak."
                ) from error

            values = (spread, slippage, commission)

            if not all(math.isfinite(v) and v >= 0 for v in values):
                raise ValueError(
                    f"{symbol}: xarajat qiymati noto'g'ri."
                )

            missing = [
                name for name, value in zip(
                    ["slippage", "commission"],
                    raw[1:]
                )
                if not value
            ]

            if missing:
                print(
                    f"[COST PARTIAL] {symbol}: "
                    f"{', '.join(missing)} kiritilmagan; "
                    "hisobda vaqtincha 0 deb olindi."
                )

            costs[symbol] = {
                "spread_price": spread,
                "slippage_price": slippage,
                "commission_r": commission,
            }

    return costs



def load_trades():
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
            raise ValueError("Savdoda symbol mavjud emas.")

        try:
            gross = float(trade["r_multiple"])
            entry = float(trade["entry"])
            stop = float(trade["stop_loss"])
        except (KeyError, ValueError, TypeError) as error:
            raise ValueError(
                f"{symbol}: savdo qiymatlari noto'g'ri."
            ) from error

        risk = abs(entry - stop)

        if (
            not all(math.isfinite(v) for v in (gross, entry, stop))
            or entry <= 0
            or risk <= 0
        ):
            raise ValueError(
                f"{symbol}: entry, SL yoki R qiymati noto'g'ri."
            )

        grouped[symbol].append({
            "trade": trade,
            "gross": gross,
            "risk": risk,
        })

    for items in grouped.values():
        items.sort(
            key=lambda item: item["trade"]["entry_time"]
        )

    return grouped


def write_trade_report(grouped, broker_costs):
    fields = [
        "symbol", "side", "entry_time", "exit_time",
        "outcome", "entry", "stop_loss",
        "risk_distance_price", "gross_r",
        "base_cost_r", "base_net_r",
        "high_cost_r", "high_net_r",
        "broker_spread_price", "broker_slippage_price",
        "broker_commission_r", "broker_cost_r",
        "broker_net_r",
    ]

    with open(
        TRADE_OUTPUT, "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()

        for symbol, items in sorted(grouped.items()):
            broker = broker_costs.get(symbol)

            for item in items:
                trade = item["trade"]
                gross = item["gross"]
                risk = item["risk"]

                broker_cost = None

                if broker:
                    broker_cost = calculate_trade_cost_r(
                        broker["spread_price"],
                        broker["slippage_price"],
                        broker["commission_r"],
                        risk,
                    )

                writer.writerow({
                    "symbol": symbol,
                    "side": trade["side"],
                    "entry_time": trade["entry_time"],
                    "exit_time": trade["exit_time"],
                    "outcome": trade["outcome"],
                    "entry": trade["entry"],
                    "stop_loss": trade["stop_loss"],
                    "risk_distance_price": round(risk, 6),
                    "gross_r": round(gross, 4),
                    "base_cost_r": 0.05,
                    "base_net_r": round(gross - 0.05, 4),
                    "high_cost_r": 0.10,
                    "high_net_r": round(gross - 0.10, 4),
                    "broker_spread_price": (
                        broker["spread_price"] if broker else ""
                    ),
                    "broker_slippage_price": (
                        broker["slippage_price"] if broker else ""
                    ),
                    "broker_commission_r": (
                        broker["commission_r"] if broker else ""
                    ),
                    "broker_cost_r": (
                        round(broker_cost, 5)
                        if broker_cost is not None else ""
                    ),
                    "broker_net_r": (
                        round(gross - broker_cost, 5)
                        if broker_cost is not None else ""
                    ),
                })


def write_summary(grouped, broker_costs):
    fields = [
        "symbol", "scenario", "trades",
        "average_risk_price",
        "break_even_total_cost_price",
        "average_cost_r", "total_net_r",
        "average_net_r", "win_rate",
        "profit_factor", "max_drawdown_r",
    ]

    all_items = []

    with open(
        SUMMARY_OUTPUT, "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()

        for symbol, items in sorted(grouped.items()):
            all_items.extend(items)

            gross = [item["gross"] for item in items]
            risks = [item["risk"] for item in items]

            break_even = calculate_break_even_price_cost(
                gross, risks
            )

            scenarios = {
                name: [cost] * len(items)
                for name, cost in COST_SCENARIOS.items()
            }

            broker = broker_costs.get(symbol)

            if broker:
                scenarios["BROKER_INPUT"] = [
                    calculate_trade_cost_r(
                        broker["spread_price"],
                        broker["slippage_price"],
                        broker["commission_r"],
                        item["risk"],
                    )
                    for item in items
                ]

            for scenario, costs in scenarios.items():
                result = calculate_results(gross, costs)

                writer.writerow({
                    "symbol": symbol,
                    "scenario": scenario,
                    "trades": result["trades"],
                    "average_risk_price": round(
                        sum(risks) / len(risks), 6
                    ),
                    "break_even_total_cost_price": (
                        round(break_even, 6)
                        if break_even is not None else ""
                    ),
                    "average_cost_r": round(
                        result["average_cost_r"], 5
                    ),
                    "total_net_r": round(
                        result["total_net_r"], 4
                    ),
                    "average_net_r": round(
                        result["average_net_r"], 4
                    ),
                    "win_rate": round(
                        result["win_rate"], 2
                    ),
                    "profit_factor": (
                        round(result["profit_factor"], 4)
                        if math.isfinite(result["profit_factor"])
                        else "inf"
                    ),
                    "max_drawdown_r": round(
                        result["max_drawdown_r"], 4
                    ),
                })

        all_items.sort(
            key=lambda item: item["trade"]["entry_time"]
        )

        gross = [item["gross"] for item in all_items]

        scenarios = {
            name: [cost] * len(all_items)
            for name, cost in COST_SCENARIOS.items()
        }

        # Umumiy broker natijasi faqat barcha instrumentlarda
        # broker xarajatlari kiritilgan bo'lsa hisoblanadi.
        if all(
            symbol in broker_costs
            for symbol in grouped
        ):
            scenarios["BROKER_INPUT"] = [
                calculate_trade_cost_r(
                    broker_costs[item["trade"]["symbol"]]["spread_price"],
                    broker_costs[item["trade"]["symbol"]]["slippage_price"],
                    broker_costs[item["trade"]["symbol"]]["commission_r"],
                    item["risk"],
                )
                for item in all_items
            ]

        for scenario, costs in scenarios.items():
            result = calculate_results(gross, costs)

            writer.writerow({
                "symbol": "ALL",
                "scenario": scenario,
                "trades": result["trades"],
                "average_risk_price": "",
                "break_even_total_cost_price": "",
                "average_cost_r": round(
                    result["average_cost_r"], 5
                ),
                "total_net_r": round(
                    result["total_net_r"], 4
                ),
                "average_net_r": round(
                    result["average_net_r"], 4
                ),
                "win_rate": round(
                    result["win_rate"], 2
                ),
                "profit_factor": (
                    round(result["profit_factor"], 4)
                    if math.isfinite(result["profit_factor"])
                    else "inf"
                ),
                "max_drawdown_r": round(
                    result["max_drawdown_r"], 4
                ),
            })


def main():
    os.makedirs("backtest_results", exist_ok=True)

    grouped = load_trades()
    broker_costs = load_broker_costs()

    write_trade_report(grouped, broker_costs)
    write_summary(grouped, broker_costs)

    total = sum(len(items) for items in grouped.values())

    print("\n===== V6 XARAJAT TAHLILI =====")
    print("Jami savdolar:", total)
    print("Instrumentlar:", len(grouped))

    for symbol, items in sorted(grouped.items()):
        gross = [item["gross"] for item in items]
        risks = [item["risk"] for item in items]

        print(f"\n[{symbol}] Savdolar: {len(items)}")

        break_even = calculate_break_even_price_cost(
            gross, risks
        )

        print(
            "Zararsizlik uchun maksimal umumiy "
            "narx xarajati:",
            (
                round(break_even, 6)
                if break_even is not None
                else "Aniqlanmadi"
            ),
        )

        for scenario, cost in COST_SCENARIOS.items():
            result = calculate_results(
                gross, [cost] * len(gross)
            )
            print(
                f"{scenario}: "
                f"Net={result['total_net_r']:.2f}R | "
                f"PF={result['profit_factor']:.3f} | "
                f"WR={result['win_rate']:.2f}% | "
                f"DD={result['max_drawdown_r']:.2f}R"
            )

        if symbol in broker_costs:
            broker = broker_costs[symbol]
            costs = [
                calculate_trade_cost_r(
                    broker["spread_price"],
                    broker["slippage_price"],
                    broker["commission_r"],
                    item["risk"],
                )
                for item in items
            ]
            result = calculate_results(gross, costs)

            print(
                "BROKER_INPUT: "
                f"Net={result['total_net_r']:.2f}R | "
                f"PF={result['profit_factor']:.3f}"
            )
        else:
            print("BROKER_INPUT: qiymatlar hali kiritilmagan.")

    missing = sorted(set(grouped) - set(broker_costs))

    if missing:
        print("\nLibertex xarajatlari kiritilmagan:", ", ".join(missing))

    print("\nHisobotlar tayyor:")
    print(TRADE_OUTPUT)
    print(SUMMARY_OUTPUT)
    print(
        "\nEslatma: BROKER_INPUT faqat kiritilgan qiymatlarga asoslanadi."
    )
    print(
        "Spred — narx birligida; slippage — borish-qaytish jami "
        "narx birligida; commission_r — jami komissiya R hisobida."
    )


if __name__ == "__main__":
    main()
