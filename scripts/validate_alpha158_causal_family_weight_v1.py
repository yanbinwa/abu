#!/usr/bin/env python3
"""Evaluate causal rolling-IC family weights against frozen equal weights."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import gc
import hashlib
import json
from pathlib import Path
import shutil
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
from scripts.analyze_alpha158_long_cycle_robustness_v1 import (  # noqa: E402
    daily_spearman,
)
from scripts.backtest_alpha158_event_exit_only_2025_2026 import save_audit  # noqa: E402
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402
from scripts.validate_alpha158_history_2015_v1 import segment_metrics  # noqa: E402


KEYS = ["signal_asof", "symbol", "column"]
DEFAULT_SOURCE = Path(
    "/Users/wjy/abu/backtests/alpha158_history_2015_validation_v1_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_causal_family_weight_v1_20261007")


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False, default=str) + "\n", encoding="utf-8")


def register(output, input_paths, snapshot_paths, config):
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    frozen = output / "frozen_inputs"
    frozen.mkdir()
    for path in snapshot_paths:
        shutil.copy2(path, frozen / path.name)
    write_json(output / "registration.json", {
        "registered_at": datetime.now().astimezone().isoformat(),
        "experiment": config,
        "input_hashes": {str(path): digest(path) for path in input_paths},
        "results_observed_at_registration": False,
        "parameter_search_performed": False,
        "family_selection_performed": False,
        "automatic_admission": False,
    })


def causal_weight_history(daily_ic, config):
    families = list(config["families"])
    pivot = daily_ic.pivot(
        index="signal_asof", columns="family", values="rank_ic").sort_index()
    pivot = pivot.reindex(columns=families)
    available = pivot.shift(int(config["label_embargo_sessions"])).rolling(
        int(config["weight_lookback_sessions"]),
        min_periods=int(config["minimum_weight_history_sessions"])).mean()
    positive = available.clip(lower=float(config["negative_family_weight"]))
    total = positive.sum(axis=1)
    normalized = positive.div(total.replace(0, np.nan), axis=0)
    equal = 1.0 / len(families)
    shrink = float(config["uniform_shrinkage"])
    weights = shrink * equal + (1.0 - shrink) * normalized
    weights = weights.fillna(equal)
    if not np.allclose(weights.sum(axis=1), 1.0, rtol=0, atol=1e-10):
        raise AssertionError("family weights do not sum to one")
    return weights, available


def build_candidate(source, config, output):
    base = None
    daily_rows = []
    rank_columns = []
    for family in config["families"]:
        path = source / "families" / family / "oos_predictions.csv.gz"
        frame = pd.read_csv(
            path, usecols=[*KEYS, "target_rank", "candidate_score",
                           "train_end"], dtype={"symbol": str})
        frame = frame.sort_values(KEYS, kind="mergesort").reset_index(drop=True)
        if base is None:
            base = frame[[*KEYS, "target_rank", "train_end"]].copy()
        elif not base[KEYS].equals(frame[KEYS]):
            raise ValueError("family prediction keys differ: " + family)
        rank = frame.groupby("signal_asof", sort=False).candidate_score.rank(
            method="average", pct=True).to_numpy(dtype=np.float32) - np.float32(.5)
        column = "rank_" + family
        base[column] = rank
        rank_columns.append(column)
        daily = daily_spearman(frame, "candidate_score")
        daily["family"] = family
        daily_rows.append(daily)
        del frame
        gc.collect()
    daily_ic = pd.concat(daily_rows, ignore_index=True)
    weights, available = causal_weight_history(daily_ic, config)
    score = np.zeros(len(base), dtype=np.float64)
    for family, column in zip(config["families"], rank_columns):
        row_weight = base.signal_asof.map(weights[family]).to_numpy(dtype=float)
        score += base[column].to_numpy(dtype=float) * row_weight
    base["causal_family_rank"] = score
    evaluation = base.signal_asof.between(
        config["evaluation_start_date"], config["evaluation_end_date"])
    evaluated_base = base.loc[evaluation].reset_index(drop=True)
    equal_score = evaluated_base[rank_columns].mean(axis=1).to_numpy(dtype=float)
    reference = pd.read_csv(
        source / "ensemble_oos_predictions.csv.gz",
        usecols=[*KEYS, "all_mean_rank"], dtype={"symbol": str})
    reference = reference.sort_values(KEYS, kind="mergesort").reset_index(drop=True)
    if not evaluated_base[KEYS].equals(reference[KEYS]):
        raise ValueError("ensemble prediction keys differ")
    if not np.allclose(equal_score, reference.all_mean_rank,
                       rtol=0, atol=2e-7):
        raise AssertionError("reconstructed equal-weight score mismatch")
    candidate = evaluated_base[
        [*KEYS, "train_end", "causal_family_rank"]].copy()
    if not (candidate.train_end < candidate.signal_asof).all():
        raise AssertionError("candidate contains non-OOS source rows")
    candidate.to_csv(
        output / "causal_family_rank_predictions.csv.gz", index=False,
        compression={"method": "gzip", "compresslevel": 3})
    daily_ic.to_csv(output / "family_daily_rank_ic.csv", index=False)
    weights.reset_index().to_csv(output / "causal_family_weights.csv", index=False)
    available.reset_index().to_csv(
        output / "available_rolling_rank_ic.csv", index=False)
    return candidate, weights


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=
                        ROOT / "configs/selection/alpha158_causal_family_weight_v1.json")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research_2015_v1"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    source_config_path = ROOT / "configs/selection/alpha158_lite_v1.json"
    policy_config_path = ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json"
    risk_config_path = ROOT / "configs/selection/risk_v1.json"
    family_paths = [args.source / "families" / family /
                    "oos_predictions.csv.gz" for family in config["families"]]
    snapshot_paths = [args.config, source_config_path, policy_config_path,
                      risk_config_path, Path(__file__)]
    inputs = [*snapshot_paths,
              ROOT / "scripts/backtest_alpha158_lite_low_turnover_v3.py",
              args.source / "ensemble_oos_predictions.csv.gz", *family_paths]
    register(args.output, inputs, snapshot_paths, config)
    candidate, weights = build_candidate(args.source, config, args.output)
    source = load_alpha158_lite_config(source_config_path)
    policy = load_alpha158_lite_low_turnover_config(policy_config_path)
    risk = load_risk_config(risk_config_path)
    scores = rank_frame(
        candidate.rename(columns={"causal_family_rank": "alpha_score"}),
        "alpha_score", max(policy.entry_rank_limit,
                           policy.retention_rank_limit))
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20110101,
        end_date=config["evaluation_end_date"])
    results, segments, annual_frames = [], [], []
    for cost in config["costs_bps"]:
        result, audit = run_low_turnover(
            panel, scores, replace(source, label_slippage_bps=float(cost)),
            policy, risk, config["evaluation_end_date"],
            review_overlay=CostAwareReview(suppress_rank_exits=True))
        directory = args.output / "causal_{}bp".format(int(cost))
        save_audit(directory, audit)
        full = segment_metrics(
            audit["curve"], config["evaluation_start_date"],
            config["evaluation_end_date"])
        result.update(full)
        result.update({"arm": "causal_family_rank",
                       "slippage_bps": float(cost)})
        results.append(result)
        for era, bounds in (
                ("2015_2019", (20150101, 20191231)),
                ("2020_2022", (20200101, 20221231)),
                ("2023_2026", (20230101, 20260930))):
            metric = segment_metrics(audit["curve"], *bounds)
            metric.update({"era": era, "slippage_bps": float(cost)})
            segments.append(metric)
        annual = annual_returns(audit["curve"])
        annual["slippage_bps"] = float(cost)
        annual_frames.append(annual)
        print(json.dumps({
            "cost": cost, "return_pct": full["return_pct"],
            "cagr_pct": full["cagr_pct"],
            "max_drawdown_pct": full["max_drawdown_pct"],
            "es95": full["daily_expected_shortfall_95_pct"],
            "average_exposure_pct": full["average_exposure_pct"],
        }), flush=True)
    results = pd.DataFrame(results)
    segments = pd.DataFrame(segments)
    annual = pd.concat(annual_frames, ignore_index=True)
    results.to_csv(args.output / "portfolio_results.csv", index=False)
    segments.to_csv(args.output / "segment_results.csv", index=False)
    annual.to_csv(args.output / "annual_returns.csv", index=False)
    base = pd.read_csv(args.source / "portfolio_results.csv")
    base = base[base.arm.eq("all_mean_rank")].set_index("slippage_bps")
    comparisons = []
    for row in results.itertuples(index=False):
        reference = base.loc[row.slippage_bps]
        comparisons.append({
            "slippage_bps": row.slippage_bps,
            "return_delta_pp": row.return_pct-reference.return_pct,
            "cagr_delta_pp": row.cagr_pct-reference.cagr_pct,
            "max_drawdown_improvement_pp": (
                row.max_drawdown_pct-reference.max_drawdown_pct),
            "es95_improvement_pp": (
                row.daily_expected_shortfall_95_pct-
                reference.daily_expected_shortfall_95_pct),
            "average_exposure_delta_pp": (
                row.average_exposure_pct-reference.average_exposure_pct),
        })
    comparisons = pd.DataFrame(comparisons)
    comparisons.to_csv(args.output / "comparisons.csv", index=False)
    primary = results[results.slippage_bps.eq(25.0)].iloc[0]
    reference = base.loc[25.0]
    delta = comparisons[comparisons.slippage_bps.eq(25.0)].iloc[0]
    weight_eval = weights[weights.index >= config["evaluation_start_date"]]
    lines = ["# Alpha158 因果滚动IC家族权重 v1", "",
             "七个因子族始终保留；权重只使用滞后21个交易日、过去252日可获得的Rank IC，并向等权收缩50%。", "",
             "该历史已被观察，结果不能自动进入模拟盘。", "",
             "|版本|累计收益|CAGR|最大回撤|ES95|平均仓位|",
             "|---|---:|---:|---:|---:|---:|",
             f"|固定等权|{reference.return_pct:+.2f}%|{reference.cagr_pct:+.2f}%|{reference.max_drawdown_pct:.2f}%|{reference.daily_expected_shortfall_95_pct:.3f}%|{reference.average_exposure_pct:.2f}%|",
             f"|因果动态权重|{primary.return_pct:+.2f}%|{primary.cagr_pct:+.2f}%|{primary.max_drawdown_pct:.2f}%|{primary.daily_expected_shortfall_95_pct:.3f}%|{primary.average_exposure_pct:.2f}%|",
             "", f"25bp收益增量：{delta.return_delta_pp:+.2f}pp；回撤改善：{delta.max_drawdown_improvement_pp:+.2f}pp。", "",
             "平均家族权重：", ""]
    for family, value in weight_eval.mean().sort_values(ascending=False).items():
        lines.append(f"- `{family}`：{value:.4f}")
    lines += ["", "成本、分时期和逐日权重见CSV。", ""]
    (args.output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(args.output / "completion.json", {
        "status": "COMPLETE", "research_status": config["research_status"],
        "parameter_search_performed": False,
        "family_selection_performed": False,
        "automatic_admission": False, "new_strategy_selected": False,
    })


if __name__ == "__main__":
    main()
