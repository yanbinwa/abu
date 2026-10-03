#!/usr/bin/env python3
"""Run frozen PIT Alpha158-lite expanding walk-forward research."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    ALPHA158_LITE_FEATURES, Alpha158LiteFeatureEngine, Alpha158LiteModel,
    load_alpha158_lite_config,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuTrialRegistry import (  # noqa: E402
    read_trial_registry, register_trial,
)
from abupy.AlphaBu.ABuWalkForward import (  # noqa: E402
    PurgedWalkForward, WalkForwardConfig,
)


def _register_once(path, trial_id, hypothesis, configuration,
                   status="REGISTERED", observed_metrics=None):
    records = read_trial_registry(path)
    matches = [item for item in records if item["trial_id"] == trial_id]
    if matches:
        if (matches[0]["hypothesis"] != hypothesis or
                matches[0]["configuration"] != configuration):
            raise ValueError("registered alpha158-lite trial differs")
        return matches[0]
    return register_trial(path, trial_id, hypothesis, configuration,
                          status=status, observed_metrics=observed_metrics)


def daily_rank_ic(frame, score_column):
    rows = []
    labeled = frame.dropna(subset=[score_column, "excess_return_20d"])
    for date, group in labeled.groupby("signal_asof", sort=True):
        if (len(group) < 20 or group[score_column].nunique() < 2 or
                group.excess_return_20d.nunique() < 2):
            continue
        value = group[score_column].corr(
            group.excess_return_20d, method="spearman")
        if np.isfinite(value):
            rows.append({"signal_asof": int(date), "ic": float(value)})
    return pd.DataFrame(rows, columns=["signal_asof", "ic"])


def daily_selection_uplift(frame, score_column, topk=10):
    rows = []
    labeled = frame.dropna(subset=[score_column, "excess_return_20d"])
    for date, group in labeled.groupby("signal_asof", sort=True):
        if len(group) < topk:
            continue
        selected = group.nlargest(topk, score_column)
        rows.append({
            "signal_asof": int(date), "candidate_count": int(len(group)),
            "candidate_excess_return": float(group.excess_return_20d.mean()),
            "selected_excess_return": float(
                selected.excess_return_20d.mean()),
            "uplift": float(selected.excess_return_20d.mean()-
                            group.excess_return_20d.mean()),
        })
    return pd.DataFrame(rows)


def walk_forward_predictions(engine, signal_days, split_config, output_dir):
    signal_days = np.asarray(signal_days, dtype=int)
    signal_dates = engine.panel.dates[signal_days]
    splitter = PurgedWalkForward(split_config)
    config = engine.config
    training_cache = {}
    predictions = []
    manifests = []
    for fold in splitter.split(signal_dates, engine.panel.dates):
        train_positions = fold.train_indices[::config.training_date_stride]
        train_days = signal_days[train_positions]
        train_frames = []
        for day in train_days:
            day = int(day)
            if day not in training_cache:
                training_cache[day] = engine.snapshot(day, include_labels=True)
            train_frames.append(training_cache[day])
        training = pd.concat(train_frames, ignore_index=True)
        model = Alpha158LiteModel(config).fit(
            training, engine.panel.dates[train_days])
        manifest = dict(model.manifest)
        manifest.update({
            "fold": int(fold.fold),
            "validation_start": int(fold.validation_start),
            "validation_end": int(fold.validation_end),
            "test_start": int(fold.test_start),
            "test_end": int(fold.test_end),
        })
        manifests.append(manifest)
        for position in fold.test_indices:
            day = int(signal_days[position])
            snapshot = engine.snapshot(day, include_labels=True)
            if snapshot.empty:
                continue
            output = snapshot[[
                "signal_asof", "symbol", "column", "baseline_score",
                "excess_return_20d", "target_rank",
            ]].copy()
            output["alpha_score"] = model.predict(snapshot)
            output["fold"] = int(fold.fold)
            output["train_end"] = int(fold.train_end)
            output["test_start"] = int(fold.test_start)
            output["test_end"] = int(fold.test_end)
            predictions.append(output)
        print(json.dumps({
            "fold": fold.fold, "train_dates_sampled": len(train_days),
            "train_rows": manifest["train_rows"],
            "test_start": fold.test_start, "test_end": fold.test_end,
        }, ensure_ascii=False), flush=True)
    if not predictions:
        raise RuntimeError("walk-forward produced no alpha158-lite predictions")
    result = pd.concat(predictions, ignore_index=True)
    if result.duplicated(["signal_asof", "symbol"]).any():
        raise AssertionError("alpha158-lite test folds overlap")
    (output_dir/"fold_manifests.json").write_text(
        json.dumps(manifests, ensure_ascii=False, indent=2)+"\n",
        encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--config", type=Path, default=
                        ROOT/"configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_lite_v1"))
    parser.add_argument("--start-date", type=int, default=20220101)
    parser.add_argument("--end-date", type=int, default=20260930)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    config = load_alpha158_lite_config(args.config)
    split_config = WalkForwardConfig(
        minimum_train_dates=config.minimum_train_dates,
        validation_dates=config.validation_dates,
        test_dates=config.test_dates,
        label_horizon_sessions=config.label_horizon_sessions,
        embargo_sessions=0,
    )
    registration_config = {
        "strategy": asdict(config), "split": asdict(split_config),
        "features": list(ALPHA158_LITE_FEATURES),
        "start_date": args.start_date, "end_date": args.end_date,
        "parameter_search": False,
    }
    registry = args.output_dir/"trial_registry.jsonl"
    trial_id = "alpha158-lite-v1-frozen-20261003"
    hypothesis = (
        "A strict PIT broad Shenzhen universe ranked by regularized daily "
        "price, volatility, liquidity and industry features has positive "
        "out-of-sample rank IC and improves a costed top-k buffer portfolio.")
    registration = _register_once(
        registry, trial_id, hypothesis, registration_config)

    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=args.end_date)
    engine = Alpha158LiteFeatureEngine(panel, config)
    signal_days = np.flatnonzero(
        (panel.dates >= args.start_date) & (panel.dates <= args.end_date) &
        (np.arange(len(panel.dates)) >= config.minimum_history_sessions))
    predictions = walk_forward_predictions(
        engine, signal_days, split_config, args.output_dir)
    alpha_ic = daily_rank_ic(predictions, "alpha_score")
    baseline_ic = daily_rank_ic(predictions, "baseline_score")
    alpha_uplift = daily_selection_uplift(
        predictions, "alpha_score", config.entry_top_k)
    baseline_uplift = daily_selection_uplift(
        predictions, "baseline_score", config.entry_top_k)
    ranked = predictions.sort_values(
        ["signal_asof", "alpha_score", "symbol"],
        ascending=[True, False, True], kind="mergesort")
    ranked["daily_rank"] = ranked.groupby(
        "signal_asof", sort=False).cumcount()+1
    portfolio = ranked[
        ranked.daily_rank <= config.portfolio_score_depth].copy()
    coverage = predictions.groupby("signal_asof").size()
    alpha_year = alpha_ic.assign(
        year=alpha_ic.signal_asof//10000).groupby("year").agg(
            dates=("ic", "size"), mean_ic=("ic", "mean"),
            positive_ic_rate=("ic", lambda values: float((values > 0).mean())),
        ).reset_index()
    baseline_year = baseline_ic.assign(
        year=baseline_ic.signal_asof//10000).groupby("year").agg(
            dates=("ic", "size"), mean_ic=("ic", "mean"),
            positive_ic_rate=("ic", lambda values: float((values > 0).mean())),
        ).reset_index()
    report = {
        "strategy_version": config.strategy_version,
        "role": "RESEARCH_ONLY_NOT_ADMITTED",
        "config_sha256": config.sha256,
        "registration_sha256": registration["record_sha256"],
        "feature_config_sha256": engine.feature_config_sha256,
        "label_config_sha256": engine.label_config_sha256,
        "universe_policy": "strict_pit_known_st_shenzhen_only",
        "oos_rows": int(len(predictions)),
        "oos_dates": int(predictions.signal_asof.nunique()),
        "folds": int(predictions.fold.nunique()),
        "median_candidates_per_date": float(coverage.median()),
        "minimum_candidates_per_date": int(coverage.min()),
        "maximum_candidates_per_date": int(coverage.max()),
        "alpha_rank_ic_mean": float(alpha_ic.ic.mean()),
        "alpha_rank_ic_positive_rate": float((alpha_ic.ic > 0).mean()),
        "baseline_rank_ic_mean": float(baseline_ic.ic.mean()),
        "baseline_rank_ic_positive_rate": float((baseline_ic.ic > 0).mean()),
        "alpha_top10_excess_return_uplift": float(alpha_uplift.uplift.mean()),
        "baseline_top10_excess_return_uplift": float(
            baseline_uplift.uplift.mean()),
        "parameter_search_performed": False,
        "warning": "The historical period was previously observed and the "
                   "strict sample excludes Shanghai because historical ST "
                   "status is incomplete.",
    }
    predictions.to_csv(
        args.output_dir/"oos_predictions.csv.gz", index=False,
        compression="gzip")
    portfolio.to_csv(
        args.output_dir/"portfolio_scores.csv.gz", index=False,
        compression="gzip")
    alpha_ic.to_csv(args.output_dir/"alpha_daily_rank_ic.csv", index=False)
    baseline_ic.to_csv(
        args.output_dir/"baseline_daily_rank_ic.csv", index=False)
    alpha_uplift.to_csv(
        args.output_dir/"alpha_selection_uplift.csv", index=False)
    baseline_uplift.to_csv(
        args.output_dir/"baseline_selection_uplift.csv", index=False)
    alpha_year.to_csv(args.output_dir/"alpha_year_metrics.csv", index=False)
    baseline_year.to_csv(
        args.output_dir/"baseline_year_metrics.csv", index=False)
    (args.output_dir/"research_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)+"\n",
        encoding="utf-8")
    result_hash = hashlib.sha256(json.dumps(
        report, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    _register_once(
        registry, trial_id+"-result-"+result_hash[:12],
        "Frozen historical result for "+trial_id,
        registration_config, status="OBSERVED", observed_metrics=report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
