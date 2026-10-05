#!/usr/bin/env python3
"""Compare unified exits with the frozen +1R/+2R scale-out experiment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _year_returns(path, initial_cash=1_000_000.0):
    nav = pd.read_csv(path).sort_values("date")
    nav["year"] = pd.to_numeric(nav.date).astype(int) // 10000
    rows, previous = [], float(initial_cash)
    for year, group in nav.groupby("year", sort=True):
        ending = float(group.capital.iloc[-1])
        rows.append({"year": int(year), "return_pct": (ending/previous-1)*100})
        previous = ending
    return pd.DataFrame(rows)


def _paired_block_bootstrap(baseline_path, scale_path, paths=5000,
                            block_size=10, seed=20261004):
    base = pd.read_csv(baseline_path, usecols=["date", "capital"])
    scale = pd.read_csv(scale_path, usecols=["date", "capital"])
    paired = base.merge(scale, on="date", suffixes=("_base", "_scale"))
    base_return = paired.capital_base.pct_change().fillna(0).to_numpy(float)
    scale_return = paired.capital_scale.pct_change().fillna(0).to_numpy(float)
    size = len(paired)
    rng = np.random.default_rng(seed)
    deltas = np.empty(paths, dtype=float)
    blocks = int(np.ceil(size / block_size))
    offsets = np.arange(block_size)
    for path in range(paths):
        starts = rng.integers(0, size, size=blocks)
        indices = ((starts[:, None] + offsets) % size).ravel()[:size]
        base_total = np.prod(1 + base_return[indices]) - 1
        scale_total = np.prod(1 + scale_return[indices]) - 1
        deltas[path] = (scale_total - base_total) * 100
    return {
        "paths": int(paths), "block_size": int(block_size),
        "delta_return_ci_low_pct": float(np.quantile(deltas, .025)),
        "delta_return_median_pct": float(np.median(deltas)),
        "delta_return_ci_high_pct": float(np.quantile(deltas, .975)),
        "probability_delta_positive": float((deltas > 0).mean()),
    }


def _one(root, variant, strategy):
    directory = Path(root) / strategy
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    fills = pd.read_csv(directory / "physical_fills.csv")
    dispositions = pd.read_csv(directory / "lot_dispositions.csv")
    trades = pd.read_csv(directory / "attribution_logical_trade.csv")
    sells = fills[(fills.side == "sell") & fills.status.eq("filled")]
    filled = fills[fills.status.eq("filled")]
    return {
        "variant": variant, "strategy": strategy,
        "return_pct": float(summary["return_pct"]),
        "max_drawdown_pct": float(summary["max_drawdown_pct"]),
        "daily_expected_shortfall_95_pct": float(
            summary["daily_expected_shortfall_95_pct"]),
        "average_exposure_pct": float(summary["average_exposure_pct"]),
        "filled_adds": int(summary["filled_adds"]),
        "sell_fills": len(sells),
        "reduce_fills": int(sells.position_effect.eq("REDUCE").sum()),
        "close_fills": int(sells.position_effect.eq("CLOSE").sum()),
        "total_fees_cash": float(filled[[
            "commission", "transfer_fee", "stamp_tax"]].sum().sum()),
        "total_slippage_cash": float(filled.slippage_cost.sum()),
        "trade_win_rate_pct": float((trades.realized_pnl_cash > 0).mean()*100),
        "median_trade_pnl_cash": float(trades.realized_pnl_cash.median()),
        "tp1_realized_pnl_cash": float(dispositions.loc[
            dispositions.exit_reason.eq("TAKE_PROFIT_1R"),
            "realized_pnl_cash"].sum()),
        "tp2_realized_pnl_cash": float(dispositions.loc[
            dispositions.exit_reason.eq("TAKE_PROFIT_2R"),
            "realized_pnl_cash"].sum()),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--scale-out", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--bootstrap-paths", type=int, default=5000)
    parser.add_argument("--block-size", type=int, default=10)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    years = []
    for strategy in ("vcp", "alpha158"):
        for variant, root in (("unified_exit", args.baseline),
                              ("scale_out_1r_2r", args.scale_out)):
            rows.append(_one(root, variant, strategy))
            annual = _year_returns(root / strategy / "daily_nav.csv")
            annual.insert(0, "strategy", strategy)
            annual.insert(1, "variant", variant)
            years.append(annual)
    comparison = pd.DataFrame(rows)
    annual = pd.concat(years, ignore_index=True)
    deltas = []
    bootstrap_rows = []
    for strategy in ("vcp", "alpha158"):
        base = comparison[(comparison.strategy == strategy) &
                          comparison.variant.eq("unified_exit")].iloc[0]
        scale = comparison[(comparison.strategy == strategy) &
                           comparison.variant.eq("scale_out_1r_2r")].iloc[0]
        record = {"strategy": strategy}
        for field in ("return_pct", "max_drawdown_pct",
                      "daily_expected_shortfall_95_pct", "average_exposure_pct",
                      "filled_adds", "sell_fills", "trade_win_rate_pct",
                      "median_trade_pnl_cash", "total_fees_cash",
                      "total_slippage_cash"):
            record["delta_" + field] = float(scale[field] - base[field])
        record["return_and_drawdown_improved"] = bool(
            record["delta_return_pct"] > 0 and
            record["delta_max_drawdown_pct"] > 0)
        deltas.append(record)
        bootstrap_rows.append({
            "strategy": strategy,
            **_paired_block_bootstrap(
                args.baseline / strategy / "daily_nav.csv",
                args.scale_out / strategy / "daily_nav.csv",
                paths=args.bootstrap_paths, block_size=args.block_size),
        })
    delta = pd.DataFrame(deltas)
    bootstrap = pd.DataFrame(bootstrap_rows)
    comparison.to_csv(args.output_dir / "comparison.csv", index=False)
    annual.to_csv(args.output_dir / "annual_returns.csv", index=False)
    delta.to_csv(args.output_dir / "deltas.csv", index=False)
    bootstrap.to_csv(args.output_dir / "paired_block_bootstrap.csv", index=False)
    report = {
        "experiment": "scale_out_1r_2r_v1",
        "rules": {
            "signal": "close reaches +1R/+2R; execute next eligible open",
            "cumulative_exit_fractions": [0.25, 0.50],
            "lot_size": 100,
            "remainder": "existing trailing/initial/rank/stagnation exits",
        },
        "comparison": comparison.replace({np.nan: None}).to_dict("records"),
        "deltas": delta.replace({np.nan: None}).to_dict("records"),
        "paired_block_bootstrap": bootstrap.to_dict("records"),
    }
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(comparison.to_string(index=False))
    print("\nDeltas\n" + delta.to_string(index=False))
    print("\nPaired block bootstrap\n" + bootstrap.to_string(index=False))


if __name__ == "__main__":
    main()
