#!/usr/bin/env python3
"""Robustness checks for frozen VCP context factors on the observed sample."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


PRIMARY_FACTORS = (
    "score", "industry_rank_value", "leader_rank_value",
)
PRIMARY_OUTCOME = "return_20d"


def _rank_correlation(x, y):
    xr = pd.Series(x).rank(method="average").to_numpy(dtype=float)
    yr = pd.Series(y).rank(method="average").to_numpy(dtype=float)
    xr -= xr.mean()
    yr -= yr.mean()
    denominator = np.sqrt(np.dot(xr, xr) * np.dot(yr, yr))
    return float(np.dot(xr, yr) / denominator) if denominator > 0 else np.nan


def within_date_ic(frame, factor, outcome=PRIMARY_OUTCOME, minimum=5):
    rows = []
    for trade_date, group in frame[[
            "signal_asof", factor, outcome]].dropna().groupby("signal_asof"):
        if (len(group) < minimum or group[factor].nunique() < 2 or
                group[outcome].nunique() < 2):
            continue
        value = _rank_correlation(group[factor], group[outcome])
        if np.isfinite(value):
            rows.append({"signal_asof": int(trade_date), "ic": value})
    return pd.DataFrame(rows, columns=["signal_asof", "ic"])


def bootstrap_mean(values, paths=10000, seed=20261003):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    rng = np.random.default_rng(seed)
    simulations = np.array([
        rng.choice(values, len(values), replace=True).mean()
        for _ in range(paths)
    ]) if len(values) else np.array([])
    return {
        "mean_ic": float(values.mean()) if len(values) else np.nan,
        "ci_2_5": float(np.quantile(simulations, .025)) if len(values) else np.nan,
        "ci_97_5": float(np.quantile(simulations, .975)) if len(values) else np.nan,
        "signal_date_clusters": int(len(values)),
    }


def permutation_test(frame, factor, outcome=PRIMARY_OUTCOME, paths=5000,
                     seed=20261003):
    groups = []
    for _, group in frame[["signal_asof", factor, outcome]].dropna().groupby(
            "signal_asof"):
        if (len(group) < 5 or group[factor].nunique() < 2 or
                group[outcome].nunique() < 2):
            continue
        x = pd.Series(group[factor]).rank(method="average").to_numpy(dtype=float)
        y = pd.Series(group[outcome]).rank(method="average").to_numpy(dtype=float)
        x -= x.mean(); y -= y.mean()
        denominator = np.sqrt(np.dot(x, x) * np.dot(y, y))
        if denominator > 0:
            groups.append((x, y, denominator))
    observed = float(np.mean([
        np.dot(x, y) / denominator for x, y, denominator in groups
    ])) if groups else np.nan
    rng = np.random.default_rng(seed)
    simulated = np.empty(paths, dtype=float)
    for path in range(paths):
        simulated[path] = np.mean([
            np.dot(rng.permutation(x), y) / denominator
            for x, y, denominator in groups
        ])
    return {
        "observed_mean_ic": observed,
        "positive_direction_p": float(
            (1 + np.sum(simulated >= observed)) / (paths + 1)),
        "null_percentile": float(np.mean(simulated <= observed)),
        "permutation_paths": paths, "signal_date_clusters": len(groups),
        "null_ci_2_5": float(np.quantile(simulated, .025)),
        "null_ci_97_5": float(np.quantile(simulated, .975)),
    }


def bh_adjust(p_values):
    values = np.asarray(p_values, dtype=float)
    order = np.argsort(values)
    adjusted = np.empty(len(values), dtype=float)
    ranked = values[order] * len(values) / np.arange(1, len(values) + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    adjusted[order] = np.minimum(ranked, 1.0)
    return adjusted


def factor_tables(intents):
    overall, periods, permutations = [], [], []
    period_masks = {
        "2023_2024": intents.signal_asof.between(20230101, 20241231),
        "2025_2026_to_0930": intents.signal_asof.between(20250101, 20260930),
    }
    for index, factor in enumerate(PRIMARY_FACTORS):
        values = within_date_ic(intents, factor)
        overall.append({"factor": factor, **bootstrap_mean(
            values.ic, seed=20261003 + index)})
        permutation = permutation_test(
            intents, factor, seed=20261003 + index)
        permutations.append({"factor": factor, **permutation})
        for label, mask in period_masks.items():
            sample = intents.loc[mask]
            period_values = within_date_ic(sample, factor)
            periods.append({
                "factor": factor, "period": label, "intent_count": len(sample),
                **bootstrap_mean(period_values.ic, seed=20261103 + index),
            })
        for year, sample in intents.groupby(intents.signal_asof // 10000):
            year_values = within_date_ic(sample, factor)
            periods.append({
                "factor": factor, "period": str(int(year)),
                "intent_count": len(sample),
                **bootstrap_mean(year_values.ic, seed=20261203 + index),
            })
    permutation_frame = pd.DataFrame(permutations)
    permutation_frame["fdr_q"] = bh_adjust(
        permutation_frame.positive_direction_p)
    return pd.DataFrame(overall), pd.DataFrame(periods), permutation_frame


def experiment_tables(results, annual):
    baseline = results.set_index("experiment").loc["baseline_replay"]
    comparison = results.copy()
    comparison["incremental_return_vs_baseline_pct_points"] = (
        comparison.return_pct - baseline.return_pct)
    comparison["drawdown_improvement_vs_baseline_pct_points"] = (
        comparison.max_drawdown_pct - baseline.max_drawdown_pct)
    comparison["es_improvement_vs_baseline_pct_points"] = (
        comparison.daily_expected_shortfall_95_pct -
        baseline.daily_expected_shortfall_95_pct)
    pivot = annual.pivot(index="year", columns="experiment", values="return_pct")
    rows = []
    for experiment in [item for item in pivot.columns if item != "baseline_replay"]:
        for year in pivot.index:
            rows.append({
                "experiment": experiment, "year": int(year),
                "return_pct": pivot.loc[year, experiment],
                "baseline_return_pct": pivot.loc[year, "baseline_replay"],
                "incremental_return_pct_points": (
                    pivot.loc[year, experiment] -
                    pivot.loc[year, "baseline_replay"]),
            })
    return comparison, pd.DataFrame(rows)


def market_state_periods(intents):
    work = intents.copy()
    work["period"] = np.where(
        work.signal_asof <= 20241231, "2023_2024", "2025_2026_to_0930")
    work["year"] = work.signal_asof // 10000
    parts = []
    for grouping in (["period", "market_state"], ["year", "market_state"]):
        result = work.groupby(grouping).agg(
            intent_count=("intent_id", "size"),
            signal_date_clusters=("signal_asof", "nunique"),
            mean_return_5d=("return_5d", "mean"),
            mean_return_20d=("return_20d", "mean"),
            false_breakout_5d=("false_breakout_5d", "mean"),
            hit_plus_1r_20d=("hit_plus_1r_20d", "mean"),
        ).reset_index()
        result.insert(0, "grouping", grouping[0])
        parts.append(result)
    return pd.concat(parts, ignore_index=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_context_v1"))
    parser.add_argument("--shadow-intents", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_context_shadow_v1/context_shadow_intents.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_factor_robustness_v1"))
    args = parser.parse_args()
    shadow = pd.read_csv(args.shadow_intents)
    outcomes = pd.read_csv(args.backtest_dir / "intent_forward_outcomes.csv")
    keys = ["intent_id", "signal_asof", "symbol", "market_state",
            "market_shadow_action"]
    intents = shadow.merge(outcomes, on=keys, validate="one_to_one")
    results = pd.read_csv(args.backtest_dir / "results.csv")
    annual = pd.read_csv(args.backtest_dir / "annual_block_diagnostics.csv")

    overall, periods, permutations = factor_tables(intents)
    comparison, annual_incremental = experiment_tables(results, annual)
    market = market_state_periods(intents)
    outputs = {
        "experiment_comparison.csv": comparison,
        "annual_incremental_returns.csv": annual_incremental,
        "factor_ic_overall.csv": overall,
        "factor_ic_by_period.csv": periods,
        "factor_permutation_tests.csv": permutations,
        "market_state_by_period.csv": market,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in outputs.items():
        frame.to_csv(args.output_dir / name, index=False)

    active = comparison[comparison.experiment.ne("baseline_replay")]
    report = {
        "analysis_version": "vcp_factor_robustness_v1",
        "evidence_role": "OBSERVED_SAMPLE_ROBUSTNESS_NOT_NEW_HOLDOUT",
        "intent_count": len(intents),
        "signal_date_count": int(intents.signal_asof.nunique()),
        "closed_trade_range": [
            int(results.closed_trades.min()), int(results.closed_trades.max())],
        "economic_experiments": active[[
            "experiment", "return_pct", "incremental_return_vs_baseline_pct_points",
            "max_drawdown_pct", "drawdown_improvement_vs_baseline_pct_points",
            "daily_expected_shortfall_95_pct",
            "es_improvement_vs_baseline_pct_points", "entry_clusters",
            "closed_trades", "mean_r",
        ]].to_dict("records"),
        "factor_ic": overall.to_dict("records"),
        "factor_permutation": permutations.to_dict("records"),
        "admission": "REJECT_ALL_NEW_CONTEXT_FACTORS",
        "reason_codes": [
            "ALL_CONTEXT_VARIANTS_UNDERPERFORM_BASELINE_AFTER_COSTS",
            "NO_POSITIVE_FACTOR_PASSES_FDR_Q_LE_0_10",
            "RECENT_PERIOD_ECONOMIC_PERFORMANCE_UNSTABLE",
            "INDEPENDENT_TEMPORAL_SAMPLE_REMAINS_SMALL",
        ],
        "minimum_forward_validation": {
            "months": 6, "entry_clusters": 30,
            "start": "2026-10-09_OR_FIRST_SUCCESSFULLY_ARCHIVED_SESSION",
            "parameter_changes": "PROHIBITED_DURING_FORWARD_WINDOW",
        },
    }
    (args.output_dir / "robustness_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=float) + "\n",
        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=float))


if __name__ == "__main__":
    main()
