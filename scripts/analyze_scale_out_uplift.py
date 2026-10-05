#!/usr/bin/env python3
"""Attribute portfolio uplift from a scale-out exit against a baseline."""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def _load(root):
    root = Path(root)
    allocations = pd.read_csv(root / "fill_allocations.csv")
    dispositions = pd.read_csv(root / "lot_dispositions.csv")
    fills = pd.read_csv(root / "physical_fills.csv")
    trades = pd.read_csv(root / "attribution_logical_trade.csv").set_index("trade_id")
    buys = allocations[allocations.side.eq("buy")].groupby(
        "trade_id").allocated_quantity.sum()
    closes = allocations[
        allocations.side.eq("sell") & allocations.position_effect.eq("CLOSE")
    ].merge(fills[["fill_id", "date"]], left_on="physical_fill_id",
            right_on="fill_id").groupby("trade_id").date.max()
    nav = pd.read_csv(root / "daily_nav.csv").sort_values("date")
    return allocations, dispositions, trades, buys, closes, nav


def _max_drawdown(nav):
    capital = nav.capital.to_numpy(float)
    peaks = np.maximum.accumulate(capital)
    drawdown = capital / peaks - 1
    trough = int(np.argmin(drawdown))
    peak = int(np.argmax(capital[:trough + 1]))
    return {
        "peak_date": int(nav.date.iloc[peak]),
        "peak_capital": float(capital[peak]),
        "trough_date": int(nav.date.iloc[trough]),
        "trough_capital": float(capital[trough]),
        "max_drawdown_pct": float(drawdown[trough] * 100),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    ba, bd, bt, bq, bc, bnav = _load(args.baseline)
    ca, cd, ct, cq, cc, cnav = _load(args.candidate)
    baseline_ids, candidate_ids = set(bt.index), set(ct.index)
    common = baseline_ids & candidate_ids
    scaled = set(cd.loc[cd.exit_reason.eq("TAKE_PROFIT_2R"), "trade_id"])
    common_scaled = common & scaled
    common_unscaled = common - scaled
    baseline_only = baseline_ids - candidate_ids
    candidate_only = candidate_ids - baseline_ids

    def pnl(frame, ids):
        return float(frame.realized_pnl_cash.reindex(list(ids)).fillna(0).sum())

    rows = []
    for category, ids in (
            ("common_scaled", common_scaled),
            ("common_unscaled", common_unscaled),
            ("baseline_only", baseline_only),
            ("candidate_only", candidate_only)):
        base_pnl, candidate_pnl = pnl(bt, ids), pnl(ct, ids)
        rows.append({
            "category": category, "trades": len(ids),
            "baseline_realized_pnl_cash": base_pnl,
            "candidate_realized_pnl_cash": candidate_pnl,
            "delta_realized_pnl_cash": candidate_pnl - base_pnl,
        })
    decomposition = pd.DataFrame(rows)
    final_delta = float(cnav.capital.iloc[-1] - bnav.capital.iloc[-1])
    realized_delta = float(ct.realized_pnl_cash.sum() - bt.realized_pnl_cash.sum())
    decomposition.loc[len(decomposition)] = {
        "category": "ending_open_positions_and_other",
        "trades": np.nan, "baseline_realized_pnl_cash": np.nan,
        "candidate_realized_pnl_cash": np.nan,
        "delta_realized_pnl_cash": final_delta - realized_delta,
    }

    joined = pd.DataFrame({
        "baseline_pnl": bt.realized_pnl_cash.reindex(list(common_scaled)),
        "candidate_pnl": ct.realized_pnl_cash.reindex(list(common_scaled)),
        "baseline_quantity": bq.reindex(list(common_scaled)),
        "candidate_quantity": cq.reindex(list(common_scaled)),
        "baseline_close_date": bc.reindex(list(common_scaled)),
        "candidate_close_date": cc.reindex(list(common_scaled)),
    }).dropna(subset=["baseline_pnl", "candidate_pnl"])
    joined["delta_pnl"] = joined.candidate_pnl - joined.baseline_pnl
    base_reason = bd[~bd.exit_reason.astype(str).str.startswith(
        "TAKE_PROFIT")].sort_values("fill_date").groupby(
            "trade_id").tail(1).set_index("trade_id").exit_reason
    joined["baseline_exit_reason"] = base_reason.reindex(joined.index)
    by_reason = joined.groupby("baseline_exit_reason").agg(
        trades=("delta_pnl", "size"),
        improved_trades=("delta_pnl", lambda values: int((values > 0).sum())),
        baseline_pnl=("baseline_pnl", "sum"),
        candidate_pnl=("candidate_pnl", "sum"),
        delta_pnl=("delta_pnl", "sum"),
    ).reset_index()
    pure = joined[
        joined.baseline_quantity.eq(joined.candidate_quantity) &
        joined.baseline_close_date.eq(joined.candidate_close_date)]
    pure_summary = pd.DataFrame([{
        "trades": len(pure), "improved_trades": int((pure.delta_pnl > 0).sum()),
        "worsened_trades": int((pure.delta_pnl < 0).sum()),
        "baseline_pnl": float(pure.baseline_pnl.sum()),
        "candidate_pnl": float(pure.candidate_pnl.sum()),
        "delta_pnl": float(pure.delta_pnl.sum()),
        "median_delta_pnl": float(pure.delta_pnl.median()),
    }])
    path = pd.DataFrame([
        {"variant": "unified_exit", **_max_drawdown(bnav)},
        {"variant": "scale_out_2r_50", **_max_drawdown(cnav)},
    ])
    decomposition.to_csv(args.output_dir / "uplift_decomposition.csv", index=False)
    by_reason.to_csv(args.output_dir / "scaled_trades_by_exit_reason.csv", index=False)
    joined.sort_values("delta_pnl", ascending=False).to_csv(
        args.output_dir / "common_scaled_trade_deltas.csv")
    pure_summary.to_csv(args.output_dir / "same_path_summary.csv", index=False)
    path.to_csv(args.output_dir / "drawdown_path.csv", index=False)
    print("Ending capital uplift: {:.2f}".format(final_delta))
    print(decomposition.to_string(index=False))
    print("\nBy baseline exit reason\n" + by_reason.to_string(index=False))
    print("\nSame quantity and exit date\n" + pure_summary.to_string(index=False))
    print("\nDrawdown path\n" + path.to_string(index=False))


if __name__ == "__main__":
    main()
