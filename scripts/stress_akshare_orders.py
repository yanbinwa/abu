#!/usr/bin/env python3
"""Replay saved ABU orders with stricter A-share execution assumptions."""
from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import backtest_akshare_cn_strategies as backtest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--start", default="2021-10-02")
    parser.add_argument("--end", default="2026-10-02")
    args = parser.parse_args()
    backtest.START, backtest.END = args.start, args.end
    data = args.root / "data_snapshot"
    benchmark = pd.read_csv(next(data.glob("sh000001_*")), usecols=["date"])
    rows = []
    for factor in ("monthly", "weekly"):
        for seed in (20261002, 20261003, 20261004):
            folder = (args.root / "entry_rest" if seed == 20261002 else
                      args.root / f"{factor}_seed_{seed}")
            orders = pd.read_csv(folder / f"orders_entry_{factor}.csv")
            orders["buy_type_str"] = "call"
            result = SimpleNamespace(orders_pd=orders,
                                     benchmark=SimpleNamespace(kl_pd=benchmark))
            for label, locked, slippage in (("locked_only", True, 0),
                                            ("locked_plus_25bps", True, 25)):
                curve, buys, sells, fees, _ = backtest.simulate_orders(
                    result, data, skip_locked_buys=locked, slippage_bps=slippage)
                row = {
                    "factor": factor, "seed": seed, "case": label,
                    "return_pct": round((curve.capital.iloc[-1] / backtest.INITIAL_CASH - 1) * 100, 3),
                    "max_drawdown_pct": round((curve.capital / curve.capital.cummax() - 1).min() * 100, 3),
                    "buys": buys, "sells": sells, "fees_cny": round(fees, 2),
                }
                rows.append(row)
                print(row, flush=True)
    pd.DataFrame(rows).to_csv(args.root / "portfolio_execution_sensitivity.csv", index=False)


if __name__ == "__main__":
    main()
