#!/usr/bin/env python3
"""All-stock, independent-trade check for three ABU entry signal families.

This is an event study, not a portfolio equity curve.  It follows ABU's
20/60-day breakout, fixed 5/60 moving-average, and weekly/monthly entry rules,
their skip-day cooldowns, and an NDay(20) exit.  It uses the frozen snapshot.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


NOTIONAL = 100_000


def commission(qty, price):
    value = qty * price
    return max(value * 0.00025, 5.0) + value * 0.0003


def events_for_symbol(path, holding_days=20, selected=None):
    frame = pd.read_csv(path, usecols=["date", "date_week", "open", "high", "low", "close",
                                       "pre_close", "volume"])
    n = len(frame)
    if n < 22:
        return []
    close = frame["close"]
    signals = {
        "break20": close.eq(close.rolling(20, min_periods=20).max()).to_numpy(),
        "break60": close.eq(close.rolling(60, min_periods=60).max()).to_numpy(),
    }
    fast = close.rolling(5, min_periods=5).mean()
    slow = close.rolling(60, min_periods=60).mean()
    signals["ma_5_60"] = ((fast.shift(1) <= slow.shift(1)) & (fast > slow)).to_numpy()
    dates = frame["date"].to_numpy()
    signals["monthly"] = np.r_[np.diff(dates) > 60, False]
    signals["weekly"] = frame["date_week"].to_numpy() == 4
    cooldown = {"break20": 20, "break60": 60, "ma_5_60": 61,
                "monthly": 0, "weekly": 0}
    values = frame[["date", "open", "high", "low", "pre_close", "volume"]].to_numpy()
    out = []
    for strategy, flags in signals.items():
        if selected and strategy not in selected:
            continue
        next_allowed = 0
        for signal_day in np.flatnonzero(flags):
            if signal_day < next_allowed or signal_day + holding_days + 1 >= n:
                continue
            next_allowed = signal_day + cooldown[strategy] + 1
            buy_i = signal_day if strategy in ("monthly", "weekly") else signal_day + 1
            buy = values[buy_i]
            if buy[5] <= 0 or buy[4] <= 0 or buy[1] <= 0:
                continue
            if buy[1] / buy[4] < 0.93:  # ABU default open-down cancellation
                continue
            buy_price = (buy[2] + buy[3]) / 2
            qty = int(NOTIONAL / buy_price) // 100 * 100
            if qty <= 0:
                continue
            sell_i = signal_day + holding_days + 1
            while sell_i < n and values[sell_i, 5] <= 0:
                sell_i += 1
            if sell_i >= n:
                continue
            sell = values[sell_i]
            sell_price = (sell[2] + sell[3]) / 2
            buy_cost = commission(qty, buy_price)
            sell_cost = commission(qty, sell_price)
            pnl = (sell_price - buy_price) * qty - buy_cost - sell_cost
            out.append({
                "strategy": strategy, "symbol": path.name.split("_")[0],
                "signal_date": int(values[signal_day, 0]),
                "buy_date": int(buy[0]), "sell_date": int(sell[0]),
                "buy_price": buy_price, "sell_price": sell_price, "qty": qty,
                "buy_locked": bool(buy[2] == buy[3]),
                "low_adjusted_price": bool(buy_price < 1 or sell_price < 1),
                "net_return": pnl / (buy_price * qty + buy_cost),
            })
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path,
                        default=Path("/Users/wjy/abu/backtests/akshare_cn_2025_2026"))
    parser.add_argument("--holding-days", type=int, default=20)
    parser.add_argument("--strategies", nargs="*", help="Optional signal families")
    args = parser.parse_args()
    root = args.root
    prefix = ("event_study" if args.holding_days == 20 and not args.strategies
              else "event_study_hold{}_{}".format(args.holding_days,
                    "-".join(args.strategies or ["all"])))
    data = root / "data_snapshot"
    files = [p for p in data.iterdir() if p.name.startswith(("sh", "sz"))
             and not p.name.startswith(("sh000", "sz399"))]
    benchmark = pd.read_csv(next(data.glob("sh000300_*"))).set_index("date")
    close = benchmark["close"]
    agg = defaultdict(lambda: {"count": 0, "total": 0.0, "excess": 0.0,
                               "excess_count": 0, "wins": 0})
    returns = defaultdict(list)
    symbols = defaultdict(set)
    columns = ["strategy", "symbol", "signal_date", "buy_date", "sell_date",
               "buy_price", "sell_price", "qty", "buy_locked",
               "low_adjusted_price", "net_return",
               "benchmark_return", "excess_return"]
    with (root / f"{prefix}_trades.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for i, path in enumerate(files, 1):
            for record in events_for_symbol(path, args.holding_days, args.strategies):
                buy_date, sell_date = record["buy_date"], record["sell_date"]
                benchmark_return = (float(close.at[sell_date] / close.at[buy_date] - 1)
                                    if buy_date in close.index and sell_date in close.index
                                    else np.nan)
                excess = record["net_return"] - benchmark_return
                record["benchmark_return"] = benchmark_return
                record["excess_return"] = excess
                writer.writerow(record)
                strategy = record["strategy"]
                symbols[strategy].add(record["symbol"])
                returns[strategy].append(record["net_return"])
                for period in ("all", str(buy_date)[:4], str(buy_date)[:6]):
                    bucket = agg[(strategy, period)]
                    bucket["count"] += 1
                    bucket["total"] += record["net_return"]
                    bucket["wins"] += record["net_return"] > 0
                    if np.isfinite(excess):
                        bucket["excess"] += excess
                        bucket["excess_count"] += 1
            if i % 1000 == 0:
                print(f"read {i}/{len(files)} files", flush=True)
    result = []
    yearly = []
    for strategy in sorted(returns):
        group = agg[(strategy, "all")]
        monthly = [value for (name, period), value in agg.items()
                   if name == strategy and len(period) == 6 and period.isdigit()]
        rng = np.random.default_rng(20261002)
        picks = rng.integers(0, len(monthly), size=(10000, len(monthly)))
        totals = np.array([bucket["total"] for bucket in monthly])
        counts = np.array([bucket["count"] for bucket in monthly])
        means = totals[picks].sum(axis=1) / counts[picks].sum(axis=1)
        ci_low, ci_high = np.quantile(means, [0.025, 0.975])
        result.append({
            "strategy": strategy, "trades": group["count"], "symbols": len(symbols[strategy]),
            "signal_months": len(monthly),
            "net_mean_pct": round(group["total"] / group["count"] * 100, 3),
            "net_median_pct": round(float(np.median(returns[strategy])) * 100, 3),
            "win_rate_pct": round(group["wins"] / group["count"] * 100, 3),
            "excess_mean_pct": round(group["excess"] / group["excess_count"] * 100, 3),
            "monthly_bootstrap_ci_low_pct": round(ci_low * 100, 3),
            "monthly_bootstrap_ci_high_pct": round(ci_high * 100, 3),
        })
        for (name, period), bucket in agg.items():
            if name == strategy and len(period) == 4 and period.isdigit():
                yearly.append({"strategy": strategy, "year": int(period),
                               "trades": bucket["count"],
                               "net_mean_pct": round(bucket["total"] / bucket["count"] * 100, 3),
                               "excess_mean_pct": round(bucket["excess"] / bucket["excess_count"] * 100, 3),
                               "win_rate_pct": round(bucket["wins"] / bucket["count"] * 100, 3)})
    pd.DataFrame(result).to_csv(root / f"{prefix}_summary.csv", index=False)
    pd.DataFrame(yearly).sort_values(["strategy", "year"]).to_csv(root / f"{prefix}_yearly.csv", index=False)
    print(pd.DataFrame(result).to_string(index=False))
    (root / f"{prefix}_metadata.json").write_text(json.dumps({
        "source": str(data), "notional_per_trade_cny": NOTIONAL,
        "holding_period": f"buy signal next-day midpoint; NDay({args.holding_days}) next-day midpoint",
        "fees": "ABU default CN commission function on each side",
        "portfolio_account": False,
        "benchmark": "sh000300 close-to-close matched to trade dates",
        "uncertainty": "10,000 resamples of signal months with replacement; exploratory only",
    }, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
