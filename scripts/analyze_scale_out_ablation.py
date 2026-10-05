#!/usr/bin/env python3
"""Compare frozen scale-out variants against the unified-exit baseline."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.analyze_scale_out_experiment import (
    _one, _paired_block_bootstrap, _year_returns,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--one-r", type=Path, required=True)
    parser.add_argument("--two-r", type=Path, required=True)
    parser.add_argument("--two-r-50", type=Path, required=True)
    parser.add_argument("--one-two-r", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--bootstrap-paths", type=int, default=5000)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    variants = {
        "unified_exit": args.baseline,
        "scale_out_1r_25": args.one_r,
        "scale_out_2r_25": args.two_r,
        "scale_out_2r_50": args.two_r_50,
        "scale_out_1r25_2r50": args.one_two_r,
    }
    rows, annual_rows, bootstrap_rows = [], [], []
    for strategy in ("vcp", "alpha158"):
        available = {name: root for name, root in variants.items()
                     if (root / strategy / "summary.json").exists()}
        for name, root in available.items():
            rows.append(_one(root, name, strategy))
            annual = _year_returns(root / strategy / "daily_nav.csv")
            annual.insert(0, "strategy", strategy)
            annual.insert(1, "variant", name)
            annual_rows.append(annual)
            if name != "unified_exit":
                bootstrap_rows.append({
                    "strategy": strategy, "variant": name,
                    **_paired_block_bootstrap(
                        args.baseline / strategy / "daily_nav.csv",
                        root / strategy / "daily_nav.csv",
                        paths=args.bootstrap_paths),
                })
    comparison = pd.DataFrame(rows)
    annual = pd.concat(annual_rows, ignore_index=True)
    bootstrap = pd.DataFrame(bootstrap_rows)
    deltas = []
    for strategy, group in comparison.groupby("strategy"):
        base = group[group.variant.eq("unified_exit")].iloc[0]
        for row in group[~group.variant.eq("unified_exit")].itertuples(index=False):
            deltas.append({
                "strategy": strategy, "variant": row.variant,
                "delta_return_pct": row.return_pct-base.return_pct,
                "delta_max_drawdown_pct": row.max_drawdown_pct-base.max_drawdown_pct,
                "delta_es95_pct": (row.daily_expected_shortfall_95_pct-
                                   base.daily_expected_shortfall_95_pct),
                "delta_average_exposure_pct": (
                    row.average_exposure_pct-base.average_exposure_pct),
                "delta_sell_fills": row.sell_fills-base.sell_fills,
                "delta_cost_cash": (
                    row.total_fees_cash+row.total_slippage_cash-
                    base.total_fees_cash-base.total_slippage_cash),
            })
    delta = pd.DataFrame(deltas).merge(
        bootstrap[["strategy", "variant", "delta_return_ci_low_pct",
                   "delta_return_median_pct", "delta_return_ci_high_pct",
                   "probability_delta_positive"]],
        on=["strategy", "variant"], how="left")
    comparison.to_csv(args.output_dir / "comparison.csv", index=False)
    annual.to_csv(args.output_dir / "annual_returns.csv", index=False)
    bootstrap.to_csv(args.output_dir / "paired_block_bootstrap.csv", index=False)
    delta.to_csv(args.output_dir / "deltas.csv", index=False)
    print(comparison.to_string(index=False))
    print("\nDeltas\n" + delta.to_string(index=False))


if __name__ == "__main__":
    main()
