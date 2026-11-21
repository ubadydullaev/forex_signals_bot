
import csv
import math
import os
from collections import defaultdict

from .cost_model import (
    COST_SCENARIOS,
    SLIPPAGE_STRESS,
    calculate_trade_cost_r,
    calculate_results,
)

INPUT_FILE = "backtest_results/v6_trades.csv"
BROKER_FILE = "v6/broker_costs.csv"

TRADE_OUTPUT = "backtest_results/v6_cost_results.csv"
SUMMARY_OUTPUT = "backtest_results/v6_cost_summary.csv"

TRADE_FIELDS = [
    "symbol", "side", "entry_time", "exit_time",
    "outcome", "entry", "stop_loss",
    "risk_distance_price", "gross_r",
    "broker_cost_r", "broker_net_r",
    "base_slippage_stress_cost_r",
    "base_slippage_stress_net_r",
    "high_slippage_stress_cost_r",
    "high_slippage_stress_net_r",
]

SUMMARY_FIELDS = [
    "symbol", "scenario", "trades",
    "average_risk_price", "average_cost_r",
    "total_net_r", "average_net_r", "win_rate",
    "profit_factor", "max_drawdown_r",
]


def load_broker_costs():
    if not os.path.exists(BROKER_FILE):
        print("[WARNING] Broker xarajatlari fayli topilmadi.")
        return {}

    costs = {}

    with open(BROKER_FILE, "r", newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            symbol = (row.get("symbol") or "").strip()

            if not symbol:
                continue

            raw_spread = (row.get("spread_price") or "").strip()
            raw_slippage = (row.get("slippage_price") or "").strip()
            raw_commission = (row.get("commission_r") or "").strip()

            if not raw_spread:
                print(f"[COST SKIP] {symbol}: spread kiritilmagan.")
                continue

            try:
                spread = float(raw_spread)
                slippage = float(raw_slippage) if raw_slippage else 0.0
                commission = float(raw_commission) if raw_commission else 0.0
            except ValueError as error:
                raise ValueError(
                    f"{symbol}: xarajat qiymati raqam bo'lishi kerak."
                ) from error

            values = (spread, slippage, commission)

            if not all(math.isfinite(v) and v >= 0 for v in values):
                raise ValueError(f"{symbol}: xarajat qiymati noto'g'ri.")

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

    with open(INPUT_FILE, "r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        required = {
            "symbol", "side", "entry_time", "exit_time",
            "outcome", "entry", "stop_loss", "r_multiple",
        }

        headers = set(reader.fieldnames or [])
        missing = required - headers

        if missing:
            raise ValueError(
                "Backtest CSV ustunlari yetishmaydi: "
                + ", ".join(sorted(missing))
            )

        trades = list(reader)

    # Backtest CSV bor, ammo unda savdo yo'q.
    if not trades:
        return {}

    grouped = defaultdict(list)

    for number, trade in enumerate(trades, start=2):
        symbol = (trade.get("symbol") or "").strip()

        if not symbol:
            raise ValueError(f"CSV {number}-qator: symbol bo'sh.")

        try:
            gross = float(trade["r_multiple"])
            entry = float(trade["entry"])
            stop = float(trade["stop_loss"])
        except (KeyError, ValueError, TypeError) as error:
            raise ValueError(
                f"{symbol}: CSV {number}-qatoridagi savdo qiymatlari noto'g'ri."
            ) from error

        risk = abs(entry - stop)

        if (
            not all(math.isfinite(v) for v in (gross, entry, stop))
            or entry <= 0
            or risk <= 0
        ):
            raise ValueError(
                f"{symbol}: entry, stop_loss yoki R qiymati noto'g'ri."
            )

        grouped[symbol].append({
            "trade": trade,
            "gross": gross,
            "risk": risk,
        })

    for items in grouped.values():
        items.sort(
            key=lambda item: item["trade"].get("entry_time", "")
        )

    return grouped


def calculate_broker_cost(broker, risk, extra_slippage_r=0.0):
    return (
        calculate_trade_cost_r(
            broker["spread_price"],
            broker["slippage_price"],
            broker["commission_r"],
            risk,
        )
        + extra_slippage_r
    )


def write_empty_reports():
    os.makedirs("backtest_results", exist_ok=True)

    with open(TRADE_OUTPUT, "w", newline="", encoding="utf-8") as file:
        csv.DictWriter(file, fieldnames=TRADE_FIELDS).writeheader()

    with open(SUMMARY_OUTPUT, "w", newline="", encoding="utf-8") as file:
        csv.DictWriter(file, fieldnames=SUMMARY_FIELDS).writeheader()

    print("\n[NO TRADES] Backtest natijasida savdolar topilmadi.")
    print("Xarajat tahlili o'tkazilmadi.")
    print("Bo'sh hisobot fayllari yaratildi:")
    print(TRADE_OUTPUT)
    print(SUMMARY_OUTPUT)


def format_profit_factor(value):
    if math.isfinite(value):
        return round(value, 4)
    return "inf"


def write_trade_report(grouped, broker_costs):
    with open(TRADE_OUTPUT, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=TRADE_FIELDS)
        writer.writeheader()

        for symbol, items in sorted(grouped.items()):
            broker = broker_costs.get(symbol)

            for item in items:
                trade = item["trade"]
                gross = item["gross"]
                risk = item["risk"]

                broker_cost = (
                    calculate_broker_cost(broker, risk)
                    if broker else None
                )

                base_cost = (
                    calculate_broker_cost(
                        broker, risk, SLIPPAGE_STRESS["SLIPPAGE_BASE"]
                    )
                    if broker else None
                )

                high_cost = (
                    calculate_broker_cost(
                        broker, risk, SLIPPAGE_STRESS["SLIPPAGE_HIGH"]
                    )
                    if broker else None
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
                    "broker_cost_r": (
                        round(broker_cost, 5)
                        if broker_cost is not None else ""
                    ),
                    "broker_net_r": (
                        round(gross - broker_cost, 5)
                        if broker_cost is not None else ""
                    ),
                    "base_slippage_stress_cost_r": (
                        round(base_cost, 5)
                        if base_cost is not None else ""
                    ),
                    "base_slippage_stress_net_r": (
                        round(gross - base_cost, 5)
                        if base_cost is not None else ""
                    ),
                    "high_slippage_stress_cost_r": (
                        round(high_cost, 5)
                        if high_cost is not None else ""
                    ),
                    "high_slippage_stress_net_r": (
                        round(gross - high_cost, 5)
                        if high_cost is not None else ""
                    ),
                })


def write_summary(grouped, broker_costs):
    all_items = []

    with open(SUMMARY_OUTPUT, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()

        for symbol, items in sorted(grouped.items()):
            all_items.extend(items)

            gross = [item["gross"] for item in items]
            risks = [item["risk"] for item in items]
            broker = broker_costs.get(symbol)

            scenarios = {
                name: [cost] * len(items)
                for name, cost in COST_SCENARIOS.items()
            }

            scenarios.update({
                name: [cost] * len(items)
                for name, cost in SLIPPAGE_STRESS.items()
            })

            if broker:
                scenarios["BROKER_SPREAD_COMMISSION"] = [
                    calculate_broker_cost(broker, item["risk"])
                    for item in items
                ]

                for name, extra in SLIPPAGE_STRESS.items():
                    scenarios[name + "_PLUS_BROKER"] = [
                        calculate_broker_cost(
                            broker, item["risk"], extra
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
                    "average_cost_r": round(
                        result["average_cost_r"], 5
                    ),
                    "total_net_r": round(result["total_net_r"], 4),
                    "average_net_r": round(result["average_net_r"], 4),
                    "win_rate": round(result["win_rate"], 2),
                    "profit_factor": format_profit_factor(
                        result["profit_factor"]
                    ),
                    "max_drawdown_r": round(
                        result["max_drawdown_r"], 4
                    ),
                })

        # Umumiy natija faqat barcha instrumentlarda broker xarajati
        # mavjud bo'lgandagina hisoblanadi.
        if set(grouped).issubset(set(broker_costs)):
            all_items.sort(
                key=lambda item: item["trade"].get("entry_time", "")
            )

            gross = [item["gross"] for item in all_items]

            broker_costs_all = [
                calculate_broker_cost(
                    broker_costs[item["trade"]["symbol"]],
                    item["risk"],
                )
                for item in all_items
            ]

            scenarios = {
                "BROKER_SPREAD_COMMISSION": broker_costs_all,
            }

            for name, extra in SLIPPAGE_STRESS.items():
                scenarios[name + "_PLUS_BROKER"] = [
                    cost + extra for cost in broker_costs_all
                ]

            for scenario, costs in scenarios.items():
                result = calculate_results(gross, costs)

                writer.writerow({
                    "symbol": "ALL",
                    "scenario": scenario,
                    "trades": result["trades"],
                    "average_risk_price": "",
                    "average_cost_r": round(
                        result["average_cost_r"], 5
                    ),
                    "total_net_r": round(result["total_net_r"], 4),
                    "average_net_r": round(result["average_net_r"], 4),
                    "win_rate": round(result["win_rate"], 2),
                    "profit_factor": format_profit_factor(
                        result["profit_factor"]
                    ),
                    "max_drawdown_r": round(
                        result["max_drawdown_r"], 4
                    ),
                })


def main():
    os.makedirs("backtest_results", exist_ok=True)

    grouped = load_trades()

    if not grouped:
        write_empty_reports()
        return

    broker_costs = load_broker_costs()

    write_trade_report(grouped, broker_costs)
    write_summary(grouped, broker_costs)

    total = sum(len(items) for items in grouped.values())

    print("\n===== V6 XARAJATLAR TAHLILI =====")
    print("Jami savdolar:", total)

    missing = sorted(set(grouped) - set(broker_costs))

    if missing:
        print("Broker xarajati kiritilmagan:", ", ".join(missing))

    print("\nSlippage stress taxminlari:")
    print("BASE: +0.01R / savdo")
    print("HIGH: +0.03R / savdo")
    print("Bu qiymatlar brokerning real slippage o'lchovi emas.")

    print("\nHisobotlar tayyor:")
    print(TRADE_OUTPUT)
    print(SUMMARY_OUTPUT)


if __name__ == "__main__":
    main()
