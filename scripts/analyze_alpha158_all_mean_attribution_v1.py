#!/usr/bin/env python3
"""Attribute the frozen Alpha158 all-mean-rank candidate without retuning it."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview  # noqa: E402
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.backtest_alpha158_lite_low_turnover_v3 import run_low_turnover  # noqa: E402
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402
from scripts.validate_alpha158_all_factor_utility_v1 import (  # noqa: E402
    KEYS, curve_cagr, load_ensemble,
)


DEFAULT_CONFIG = ROOT / "configs/selection/alpha158_all_factor_utility_v1.json"
DEFAULT_BACKTEST = Path(
    "/Users/wjy/abu/backtests/alpha158_all_factor_utility_interim_replay_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_all_mean_attribution_v1_20261007")


def write_json(path, payload):
    Path(path).write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False, default=str) + "\n", encoding="utf-8")


def standalone_evidence(config):
    rows = []
    for family in config["primary_families"]:
        report = json.loads((
            Path(config["primary_sources"][family]) / "report.json"
        ).read_text(encoding="utf-8"))
        factor = report["factor"]
        portfolio = report["portfolio_comparison"]
        rows.append({
            "family": family,
            "rank_ic_delta": float(factor["paired_ic_delta_mean"]),
            "rank_ic_ci_low": float(factor["paired_ic_delta_block_ci"][0]),
            "rank_ic_ci_high": float(factor["paired_ic_delta_block_ci"][1]),
            "top10_uplift_delta_pp": float(
                factor["top10_uplift_delta_mean"] * 100),
            "top10_ci_low_pp": float(
                factor["top10_uplift_delta_block_ci"][0] * 100),
            "top10_ci_high_pp": float(
                factor["top10_uplift_delta_block_ci"][1] * 100),
            "positive_year_ic_delta_count": int(
                factor["positive_year_ic_delta_count"]),
            "return_delta_pp": float(portfolio["return_delta_pct_points"]),
            "max_drawdown_delta_pp": float(
                portfolio["max_drawdown_delta_pct_points"]),
            "liquidation_return_delta_pp": float(
                portfolio["liquidation_return_delta_pct_points"]),
            "average_exposure_delta_pp": float(
                portfolio["average_exposure_delta_pct_points"]),
        })
    return pd.DataFrame(rows)


def metrics(result, curve):
    return {
        "return_pct": float(result["return_pct"]),
        "cagr_pct": curve_cagr(curve),
        "max_drawdown_pct": float(result["max_drawdown_pct"]),
        "daily_expected_shortfall_95_pct": float(
            result["daily_expected_shortfall_95_pct"]),
        "average_exposure_pct": float(result["average_exposure_pct"]),
        "liquidation_3_limits_return_pct": float(
            result["liquidation_3_limits_return_pct"]),
        "filled_buys": int(result["filled_buys"]),
        "filled_sells": int(result["filled_sells"]),
        "total_friction_pct_initial": float(
            result["total_friction_pct_initial"]),
    }


def leave_one_family_out(predictions, rank_columns, config, source, policy,
                         risk, panel, backtest, output):
    full = pd.read_csv(backtest / "portfolio_results.csv")
    full = full[(full.arm == "all_mean_rank") &
                (full.slippage_bps == 25.0)].iloc[0]
    rows = []
    for family, removed in zip(config["primary_families"], rank_columns):
        remaining = [column for column in rank_columns if column != removed]
        score = "without_{}".format(family)
        predictions[score] = predictions[remaining].mean(axis=1)
        ranked = rank_frame(
            predictions[[*KEYS, score]].rename(columns={score: "alpha_score"}),
            "alpha_score", max(
                policy.entry_rank_limit, policy.retention_rank_limit))
        result, audit = run_low_turnover(
            panel, ranked, replace(source, label_slippage_bps=25.0),
            policy, risk, int(predictions.signal_asof.max()),
            review_overlay=CostAwareReview(suppress_rank_exits=True))
        directory = output / score
        directory.mkdir(parents=True)
        audit["curve"].to_csv(directory / "daily_nav.csv", index=False)
        item = {
            "family": family,
            **metrics(result, audit["curve"]),
            "marginal_return_contribution_pp": float(
                full.return_pct - result["return_pct"]),
            "marginal_drawdown_improvement_pp": float(
                full.max_drawdown_pct - result["max_drawdown_pct"]),
            "marginal_es95_improvement_pp": float(
                full.daily_expected_shortfall_95_pct -
                result["daily_expected_shortfall_95_pct"]),
        }
        rows.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
    return pd.DataFrame(rows)


def selection_support(predictions, backtest):
    rank_columns = [column for column in predictions
                    if column.startswith("family_") and column.endswith("_rank")]
    label = {column: column[len("family_"):-len("_rank")]
             for column in rank_columns}
    selected = {}
    for arm in ("baseline", "all_mean_rank"):
        decisions = pd.read_csv(
            backtest / (arm + "_25bp") / "selection_decisions.csv",
            dtype={"symbol": str})
        decisions = decisions[decisions.order_created.astype(str).str.lower().eq(
            "true")][["signal_asof", "symbol"]].drop_duplicates()
        selected[arm] = decisions
    base_keys = set(map(tuple, selected["baseline"].to_numpy()))
    mean = selected["all_mean_rank"].merge(
        predictions[["signal_asof", "symbol", *rank_columns]],
        on=["signal_asof", "symbol"], how="left", validate="one_to_one")
    mean["selection_group"] = [
        "shared_with_baseline" if key in base_keys else "ensemble_only"
        for key in map(tuple, mean[["signal_asof", "symbol"]].to_numpy())]
    rows = []
    for group, values in mean.groupby("selection_group"):
        for column in rank_columns:
            rows.append({
                "selection_group": group, "family": label[column],
                "mean_centered_rank": float(values[column].mean()),
                "median_centered_rank": float(values[column].median()),
                "selection_count": int(len(values)),
            })
    return pd.DataFrame(rows)


def trade_summary(backtest):
    rows = []
    for arm in ("baseline", "all_mean_rank"):
        root = backtest / (arm + "_25bp")
        fills = pd.read_csv(root / "fills.csv", dtype={"symbol": str})
        fills = fills[fills.status.eq("filled")].copy()
        fills["gross_cash"] = (
            fills.quantity.astype(float) * fills.fill_price_raw.astype(float))
        fills["total_cost"] = fills[[
            "commission", "transfer_fee", "stamp_tax", "slippage_cost"
        ]].sum(axis=1)
        for year, values in fills.groupby(fills.date.astype(int) // 10000):
            for side, side_values in values.groupby("side"):
                rows.append({
                    "arm": arm, "year": int(year), "side": side,
                    "fill_count": int(len(side_values)),
                    "gross_cash": float(side_values.gross_cash.sum()),
                    "total_cost": float(side_values.total_cost.sum()),
                })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--backtest-dir", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--policy-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json")
    parser.add_argument("--risk-config", type=Path, default=
                        ROOT / "configs/selection/risk_v1.json")
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    args.output_dir.mkdir(parents=True)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    predictions, source_hashes, rank_columns = load_ensemble(config)
    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=int(predictions.signal_asof.max()))
    standalone = standalone_evidence(config)
    lofo = leave_one_family_out(
        predictions, rank_columns, config, source, policy, risk, panel,
        args.backtest_dir,
        args.output_dir)
    support = selection_support(predictions, args.backtest_dir)
    trades = trade_summary(args.backtest_dir)
    standalone.to_csv(args.output_dir / "standalone_family_evidence.csv", index=False)
    lofo.to_csv(args.output_dir / "leave_one_family_out.csv", index=False)
    support.to_csv(args.output_dir / "selection_family_support.csv", index=False)
    trades.to_csv(args.output_dir / "annual_trade_summary.csv", index=False)
    report = {
        "analysis_id": "alpha158_all_mean_attribution_v1",
        "candidate": "all_mean_rank",
        "cost_bps": 25.0,
        "attribution_method": (
            "leave-one-family-out portfolio replay plus standalone family evidence"),
        "source_hashes": source_hashes,
        "family_count": len(rank_columns),
        "warning": (
            "LOFO effects include interactions and do not add to the total uplift. "
            "They explain the frozen ensemble and are not a family-selection rule."),
        "outputs": {
            "standalone": "standalone_family_evidence.csv",
            "lofo": "leave_one_family_out.csv",
            "selection_support": "selection_family_support.csv",
            "annual_trades": "annual_trade_summary.csv",
        },
    }
    write_json(args.output_dir / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
