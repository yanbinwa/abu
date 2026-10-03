#!/usr/bin/env python3
"""Build PIT VCP features and purged walk-forward quality predictions."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuSelectionFeatures import (  # noqa: E402
    VCP_QUALITY_FEATURES, VCPFeatureSnapshotBuilder,
)
from abupy.AlphaBu.ABuSelectionLabels import VCPLabelBuilder  # noqa: E402
from abupy.AlphaBu.ABuSelectionModel import (  # noqa: E402
    load_vcp_quality_model_config, walk_forward_quality_predictions,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuTrialRegistry import (  # noqa: E402
    read_trial_registry, register_trial,
)
from abupy.AlphaBu.ABuWalkForward import WalkForwardConfig  # noqa: E402
from scripts.backtest_vcp_context_v1 import load_frozen_intents  # noqa: E402


def daily_rank_ic(frame, score, outcome, minimum_candidates=5):
    rows = []
    for date, group in frame.dropna(subset=[score, outcome]).groupby(
            "signal_asof", sort=True):
        if (len(group) < minimum_candidates or group[score].nunique() < 2 or
                group[outcome].nunique() < 2):
            continue
        value = group[score].corr(group[outcome], method="spearman")
        if np.isfinite(value):
            rows.append({"signal_asof": int(date), "ic": float(value)})
    return pd.DataFrame(rows)


def selection_summary(frame, topk=10, outcome="excess_return_20d"):
    rows = []
    for date, group in frame.groupby("signal_asof", sort=True):
        if len(group) < 2:
            continue
        count = min(int(topk), len(group))
        quality = group.nlargest(count, "quality_score")
        legacy = group.nlargest(count, "legacy_score")
        rows.append({
            "signal_asof": int(date), "candidate_count": int(len(group)),
            "selected_count": count,
            "candidate_return": float(group[outcome].mean()),
            "quality_return": float(quality[outcome].mean()),
            "legacy_return": float(legacy[outcome].mean()),
            "candidate_false_breakout": float(group.false_breakout_5d.mean()),
            "quality_false_breakout": float(quality.false_breakout_5d.mean()),
            "legacy_false_breakout": float(legacy.false_breakout_5d.mean()),
        })
    return pd.DataFrame(rows)


def _register_once(path, trial_id, hypothesis, configuration,
                   status="REGISTERED", observed_metrics=None):
    records = read_trial_registry(path)
    matches = [item for item in records if item["trial_id"] == trial_id]
    if matches:
        if (matches[0]["hypothesis"] != hypothesis or
                matches[0]["configuration"] != configuration):
            raise ValueError("registered trial differs from requested trial")
        return matches[0]
    return register_trial(path, trial_id, hypothesis, configuration,
                          status=status, observed_metrics=observed_metrics)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shadow-intents", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_context_shadow_v1/context_shadow_intents.csv"))
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_quality_rank_v1"))
    parser.add_argument("--config", type=Path, default=
                        ROOT / "configs/selection/vcp_quality_rank_v1.json")
    parser.add_argument("--start-date", type=int, default=20200101)
    parser.add_argument("--end-date", type=int, default=20260930)
    parser.add_argument("--minimum-train-dates", type=int, default=60)
    parser.add_argument("--validation-dates", type=int, default=20)
    parser.add_argument("--test-dates", type=int, default=20)
    parser.add_argument("--label-horizon", type=int, default=20)
    parser.add_argument("--return-target", default="excess_return_20d",
                        choices=("excess_return_20d", "event_path_r_60d"))
    parser.add_argument("--topk", type=int, default=10)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    model_config = load_vcp_quality_model_config(args.config)
    split_config = WalkForwardConfig(
        minimum_train_dates=args.minimum_train_dates,
        validation_dates=args.validation_dates,
        test_dates=args.test_dates,
        label_horizon_sessions=args.label_horizon,
        embargo_sessions=0,
    )
    registration_config = {
        "model": asdict(model_config), "split": asdict(split_config),
        "features": list(VCP_QUALITY_FEATURES), "topk": args.topk,
        "source_strategy": "vcp_residual_v2",
        "execution_label_entry": "t_plus_1_open",
        "return_target": args.return_target,
        "parameter_search": False,
    }
    registry_path = args.output_dir / "trial_registry.jsonl"
    trial_id = model_config.strategy_version.replace(
        "_", "-")+"-frozen-20261003"
    registration = _register_once(
        registry_path, trial_id,
        "Candidate quality predicts fewer five-day false breakouts and higher "
        "20-day excess return than the frozen equal-weight VCP score.",
        registration_config,
    )

    shadow = pd.read_csv(args.shadow_intents, dtype={"symbol": str})
    intents = load_frozen_intents(shadow)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=args.start_date, end_date=args.end_date)
    features = VCPFeatureSnapshotBuilder(
        panel, feature_version=model_config.feature_version).build(intents)
    labels = VCPLabelBuilder(
        panel, label_version=model_config.label_version).build(intents)
    dataset = features.merge(
        labels, on=["intent_id", "signal_asof", "symbol"],
        how="inner", validate="one_to_one", suffixes=("", "_label"))
    dataset = dataset[dataset.entry_executable].dropna(
        subset=["false_breakout_5d", args.return_target]
    ).reset_index(drop=True)
    features.to_csv(args.output_dir / "feature_snapshots.csv.gz",
                    index=False, compression="gzip")
    labels.to_csv(args.output_dir / "execution_labels.csv.gz",
                  index=False, compression="gzip")
    dataset.to_csv(args.output_dir / "model_dataset.csv.gz",
                   index=False, compression="gzip")
    predictions = walk_forward_quality_predictions(
        dataset, panel.dates, model_config, split_config,
        VCP_QUALITY_FEATURES, return_target=args.return_target)
    if predictions.empty:
        raise RuntimeError("walk-forward configuration produced no predictions")
    keys = ["intent_id", "signal_asof", "symbol", "legacy_score",
            "false_breakout_5d", args.return_target]
    keyed = dataset.loc[predictions.row_index.to_numpy(dtype=int), keys]
    keyed = keyed.reset_index(drop=True)
    predictions = pd.concat([
        keyed, predictions.drop(columns="row_index").reset_index(drop=True)
    ], axis=1)

    quality_ic = daily_rank_ic(
        predictions, "quality_score", args.return_target)
    legacy_ic = daily_rank_ic(
        predictions, "legacy_score", args.return_target)
    selection = selection_summary(predictions, args.topk, args.return_target)
    fold_metrics = predictions.groupby("fold").agg(
        rows=("intent_id", "size"), dates=("signal_asof", "nunique"),
        mean_target=(args.return_target, "mean"),
        mean_quality_score=("quality_score", "mean"),
        false_breakout_rate=("false_breakout_5d", "mean"),
    ).reset_index()
    year_metrics = predictions.assign(
        year=predictions.signal_asof.astype(int)//10000
    ).groupby("year").agg(
        rows=("intent_id", "size"), dates=("signal_asof", "nunique"),
        mean_target=(args.return_target, "mean"),
        false_breakout_rate=("false_breakout_5d", "mean"),
    ).reset_index()
    auc = roc_auc_score(
        predictions.false_breakout_5d.astype(int),
        predictions.false_breakout_probability)
    summary = {
        "strategy_version": model_config.strategy_version,
        "role": "RESEARCH_ONLY_NOT_ADMITTED",
        "registration_sha256": registration["record_sha256"],
        "model_config_sha256": model_config.sha256,
        "return_target": args.return_target,
        "feature_config_sha256": str(features.feature_config_sha256.iloc[0]),
        "label_config_sha256": str(labels.label_config_sha256.iloc[0]),
        "candidate_rows": int(len(dataset)),
        "candidate_dates": int(dataset.signal_asof.nunique()),
        "oos_rows": int(len(predictions)),
        "oos_dates": int(predictions.signal_asof.nunique()),
        "folds": int(predictions.fold.nunique()),
        "false_breakout_auc": float(auc),
        "quality_rank_ic_mean": float(quality_ic.ic.mean()),
        "quality_rank_ic_dates": int(len(quality_ic)),
        "legacy_rank_ic_mean_same_oos": float(legacy_ic.ic.mean()),
        "legacy_rank_ic_dates_same_oos": int(len(legacy_ic)),
        "quality_selection_target_uplift": float(
            (selection.quality_return-selection.candidate_return).mean()),
        "legacy_selection_target_uplift": float(
            (selection.legacy_return-selection.candidate_return).mean()),
        "quality_false_breakout_change": float(
            (selection.quality_false_breakout-
             selection.candidate_false_breakout).mean()),
        "legacy_false_breakout_change": float(
            (selection.legacy_false_breakout-
             selection.candidate_false_breakout).mean()),
        "parameter_search_performed": False,
        "warning": "historical period was previously observed; this is purged "
                   "walk-forward development evidence, not fresh forward evidence",
    }

    predictions.to_csv(args.output_dir / "oos_predictions.csv", index=False)
    quality_ic.assign(score="quality_score").to_csv(
        args.output_dir / "quality_daily_rank_ic.csv", index=False)
    legacy_ic.assign(score="legacy_score").to_csv(
        args.output_dir / "legacy_daily_rank_ic.csv", index=False)
    selection.to_csv(args.output_dir / "selection_uplift_by_date.csv", index=False)
    fold_metrics.to_csv(args.output_dir / "fold_metrics.csv", index=False)
    year_metrics.to_csv(args.output_dir / "year_metrics.csv", index=False)
    (args.output_dir / "research_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    result_hash = hashlib.sha256(json.dumps(
        summary, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    _register_once(
        registry_path, trial_id+"-result-"+result_hash[:12],
        "Frozen walk-forward result for "+trial_id,
        registration_config, status="OBSERVED",
        observed_metrics=summary,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
