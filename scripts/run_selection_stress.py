#!/usr/bin/env python3
"""Create causal regime, path-risk and admission reports for one frozen run."""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuResearchStatistics import (  # noqa: E402
    attribute_daily_pnl, attribute_entry_clusters, benjamini_hochberg,
    block_bootstrap, classify_market_regimes, closed_trades_from_fills,
    cluster_bootstrap_mean, evaluate_admission, top_profit_concentration,
    write_forward_registration,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402


def _drawdown(values):
    values = np.asarray(values, dtype=float)
    return float((values / np.maximum.accumulate(values) - 1).min())


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (float, np.floating)) and not math.isfinite(float(value)):
        return None
    if isinstance(value, np.generic):
        return value.item()
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--curve", type=Path, required=True)
    parser.add_argument("--fills", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--placebo-summary", type=Path)
    parser.add_argument("--paths", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20261002)
    parser.add_argument("--annual-blocks-above-median", type=int, default=0)
    parser.add_argument("--liquidation-drawdown-budget", type=float, default=0.25)
    parser.add_argument("--initial-capital", type=float, default=1_000_000.0)
    parser.add_argument("--family-p-values", type=float, nargs="*", default=[])
    parser.add_argument("--config-hash", action="append", default=[],
                        help="name=sha256; repeat for every frozen config")
    parser.add_argument("--daily-command", default="not_registered")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    curve = pd.read_csv(args.curve)
    fills = pd.read_csv(args.fills)
    if curve.date.duplicated().any() or not curve.date.is_monotonic_increasing:
        raise ValueError("curve must be one continuous, ordered path with unique dates")
    start_year = int(curve.date.iloc[0]) // 10000
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=(start_year - 2) * 10000 + 101,
        end_date=int(curve.date.iloc[-1]),
    )
    regimes = classify_market_regimes(panel)
    regimes = regimes[regimes.date.isin(curve.date)]
    daily, daily_summary = attribute_daily_pnl(
        curve, regimes, initial_capital=args.initial_capital)
    regimes.to_csv(args.output_dir / "market_regimes.csv", index=False)
    daily.to_csv(args.output_dir / "daily_state_attribution.csv", index=False)
    daily_summary.to_csv(args.output_dir / "daily_state_summary.csv", index=False)

    closed = closed_trades_from_fills(fills)
    attributed, entry_summary = attribute_entry_clusters(closed, regimes)
    attributed.to_csv(args.output_dir / "trades_closed.csv", index=False)
    entry_summary.to_csv(args.output_dir / "entry_state_summary.csv", index=False)
    values = attributed.r_multiple if attributed.r_multiple.notna().any() \
        else attributed.return_pct
    ci = cluster_bootstrap_mean(
        values, attributed.entry_cluster, samples=args.paths, seed=args.seed)

    returns = daily.daily_return.to_numpy(dtype=float)
    mc_summary, mc_paths, km = block_bootstrap(
        returns, paths=args.paths, seed=args.seed)
    mc_summary.to_csv(args.output_dir / "monte_carlo_summary.csv", index=False)
    mc_paths.to_csv(args.output_dir / "monte_carlo_paths.csv", index=False)
    for method, table in km.items():
        table.to_csv(args.output_dir / ("recovery_km_" + method + ".csv"), index=False)

    placebo_percentile = 0.0
    if args.placebo_summary and args.placebo_summary.exists():
        placebo_percentile = float(json.loads(
            args.placebo_summary.read_text()).get("actual_percentile", 0.0) or 0.0)
    p_family = [ci.get("p_nonpositive", 1.0)] + list(args.family_p_values)
    q_value = float(benjamini_hochberg(p_family)[0])
    concentration = top_profit_concentration(closed.profit) if len(closed) else np.nan
    known_states = attributed.loc[
        attributed.market_state != "UNKNOWN", "market_state"].nunique()
    liquidation_column = ("liquidation_nav_3_limits"
                          if "liquidation_nav_3_limits" in curve else None)
    liquidation_dd = (_drawdown(np.r_[args.initial_capital,
                                      curve[liquidation_column].to_numpy()])
                      if liquidation_column else np.nan)
    liquidation_ok = bool(np.isfinite(liquidation_dd) and
                          liquidation_dd >= -args.liquidation_drawdown_budget)
    admission = evaluate_admission(
        closed_trades=len(closed),
        entry_clusters=attributed.entry_cluster.nunique() if len(attributed) else 0,
        known_states=int(known_states), cluster_ci_lower=ci.get("lower", np.nan),
        placebo_percentile=placebo_percentile,
        annual_blocks_above_median=args.annual_blocks_above_median,
        top5_contribution=concentration, q_value=q_value,
        liquidation_drawdown_ok=liquidation_ok,
        forward_candidate=True,
    )
    report = {
        "strategy": args.strategy, "status": admission.status,
        "reasons": admission.reasons, "admission_metrics": admission.metrics,
        "cluster_bootstrap": ci, "raw_p_value": p_family[0], "fdr_q_value": q_value,
        "top5_profit_contribution": concentration,
        "placebo_v2_percentile": placebo_percentile,
        "liquidation_nav_available": liquidation_column is not None,
        "liquidation_drawdown": liquidation_dd,
        "known_state_coverage": int(known_states),
        "monte_carlo_interpretation": (
            "Path-risk diagnostic only. Simulated paths are not independent market samples "
            "and do not establish stock-selection alpha."),
        "bootstrap_paths_per_method": args.paths,
    }
    report = _json_safe(report)
    (args.output_dir / "admission_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")

    hashes = dict(item.split("=", 1) for item in args.config_hash)
    if admission.status == "candidate":
        write_forward_registration(
            args.output_dir / "forward_registration.json", args.strategy,
            hashes, args.daily_command,
            ["PIT universe", "raw and adjusted bars", "corporate actions",
             "ST, suspension and price-limit state"],
            "halt new entries, preserve exits, and open an incident report",
            start_after="2026-10-02",
        )
    else:
        (args.output_dir / "forward_registration_blocked.json").write_text(
            json.dumps({"strategy": args.strategy, "status": "NOT_ELIGIBLE",
                        "reasons": admission.reasons},
                       ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
