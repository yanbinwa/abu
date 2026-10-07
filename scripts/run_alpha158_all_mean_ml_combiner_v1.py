#!/usr/bin/env python3
"""Run the preregistered A0 equal-weight versus A1 simplex combiner screen."""
from __future__ import annotations

import argparse
import gc
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuWalkForward import (  # noqa: E402
    PurgedWalkForward, WalkForwardConfig,
)
from abupy.MLBu.ABuMLContracts import (  # noqa: E402
    ExperimentRegistration, MLContractError, canonical_config_sha256,
    load_experiment_config,
)
from abupy.MLBu.ABuMLDecision import decide_historical_screen  # noqa: E402
from abupy.MLBu.ABuMLFamilyOOSAudit import (  # noqa: E402
    FAMILIES, audit_family_set, file_sha256,
)
from abupy.MLBu.adapters.ABuAllMeanRankMLAdapter import (  # noqa: E402
    FAMILY_RANK_COLUMNS,
)
from abupy.MLBu.models.ABuSimplexCombiner import (  # noqa: E402
    EqualWeightCombiner, SimplexCombiner,
)
from scripts.run_alpha158_ml_score_replacement_v1 import (  # noqa: E402
    evidence_for, prediction_key_hash, run_accounts, write_json,
)


DEFAULT_FAMILY_ROOT = Path(
    "/Users/wjy/abu/backtests/alpha158_history_2015_validation_v1_20261007")
DEFAULT_DATASET_REPORT = Path(
    "/Users/wjy/abu/data/selection_research_2015_v1/dataset_report.json")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_all_mean_ml_combiner_v1_20261007")
ARM_COLUMNS = {"all_mean_rank_a0": "a0_score",
               "all_mean_rank_a1": "a1_score"}


def load_family_matrix(root, audit):
    base = None
    for family in FAMILIES:
        directory = Path(root)/"families"/family
        frame = pd.read_csv(
            directory/"oos_predictions.csv.gz", dtype={"symbol": str},
            usecols=["signal_asof", "symbol", "column", "target_rank",
                     "candidate_score", "fold"])
        frame.sort_values(["signal_asof", "symbol"], kind="mergesort",
                          inplace=True, ignore_index=True)
        rank_column = "family_{}_rank".format(family)
        frame[rank_column] = frame.groupby(
            "signal_asof", sort=False).candidate_score.rank(
                method="average", pct=True).to_numpy(dtype=float)-.5
        manifest_column = "family_{}_source_manifest_id".format(family)
        fold_column = "family_{}_fold".format(family)
        frame[fold_column] = frame.fold.astype(int)
        frame[manifest_column] = [
            "{}-fold-{:02d}".format(family, int(value))
            for value in frame.fold]
        if base is None:
            base = frame[["signal_asof", "symbol", "column", "target_rank",
                          rank_column, manifest_column, fold_column]].copy()
        else:
            if not base[["signal_asof", "symbol", "column"]].equals(
                    frame[["signal_asof", "symbol", "column"]]):
                raise MLContractError("family keys changed after M7 audit")
            base[rank_column] = frame[rank_column].to_numpy()
            base[manifest_column] = frame[manifest_column].to_numpy()
            base[fold_column] = frame[fold_column].to_numpy()
        del frame
        gc.collect()
    if len(base) != audit["common_rows"]:
        raise MLContractError("loaded family row count differs from M7 audit")
    return base


def generate_combiner_predictions(frame, panel_dates, config, output):
    walk = config["walk_forward"]
    split = WalkForwardConfig(
        minimum_train_dates=int(walk["minimum_train_dates"]),
        validation_dates=int(walk["validation_dates"]),
        test_dates=int(walk["test_dates"]),
        label_horizon_sessions=int(walk["label_horizon_sessions"]),
        embargo_sessions=int(walk["embargo_sessions"]))
    dates = np.sort(frame.signal_asof.unique().astype(int))
    by_date = {int(date): group for date, group in
               frame.groupby("signal_asof", sort=False)}
    predictions, manifests = [], []
    for fold in PurgedWalkForward(split).split(dates, panel_dates):
        train_dates = dates[fold.train_indices][::int(walk["train_date_step"])]
        valid_dates = dates[fold.validation_indices]
        train = pd.concat([by_date[int(date)] for date in train_dates],
                          ignore_index=True).dropna(subset=["target_rank"])
        valid = pd.concat([by_date[int(date)] for date in valid_dates],
                          ignore_index=True).dropna(subset=["target_rank"])
        if train.empty or valid.empty:
            raise MLContractError("combiner fold lacks mature labels")
        a0 = EqualWeightCombiner()
        a1 = SimplexCombiner().fit(train, valid)
        fold_rows = []
        for position in fold.test_indices:
            snapshot = by_date[int(dates[position])]
            values = snapshot[["signal_asof", "symbol", "column",
                               "target_rank"]].copy()
            values["a0_score"] = a0.predict(snapshot)
            values["a1_score"] = a1.predict(snapshot)
            values["fold"] = int(fold.fold)
            fold_rows.append(values)
        result = pd.concat(fold_rows, ignore_index=True)
        predictions.append(result)
        manifests.append({
            "fold": int(fold.fold), "train_start": int(fold.train_start),
            "train_end": int(fold.train_end),
            "train_label_end": int(fold.train_label_end),
            "validation_start": int(fold.validation_start),
            "validation_end": int(fold.validation_end),
            "validation_label_end": int(fold.validation_label_end),
            "test_start": int(fold.test_start), "test_end": int(fold.test_end),
            "train_rows": int(len(train)), "validation_rows": int(len(valid)),
            "prediction_rows": int(len(result)), "a0": a0.manifest,
            "a1": a1.manifest,
        })
        print(json.dumps({
            "fold": int(fold.fold), "prediction_rows": len(result),
            "lambda": a1.selection["lambda"],
        }), flush=True)
    if not predictions:
        raise MLContractError("combiner produced no OOS folds")
    combined = pd.concat(predictions, ignore_index=True).sort_values(
        ["signal_asof", "symbol"], kind="mergesort").reset_index(drop=True)
    if combined.duplicated(["signal_asof", "symbol"]).any():
        raise MLContractError("combiner OOS folds overlap")
    combined.to_csv(
        output/"oos_predictions.csv.gz", index=False,
        compression={"method": "gzip", "compresslevel": 3, "mtime": 0})
    write_json(output/"fold_manifests.json", manifests)
    return combined


def expected_combiner_keys(frame, panel_dates, config):
    """Derive the full OOS key set without fitting or reading outcomes."""
    walk = config["walk_forward"]
    split = WalkForwardConfig(
        minimum_train_dates=int(walk["minimum_train_dates"]),
        validation_dates=int(walk["validation_dates"]),
        test_dates=int(walk["test_dates"]),
        label_horizon_sessions=int(walk["label_horizon_sessions"]),
        embargo_sessions=int(walk["embargo_sessions"]))
    dates = np.sort(frame.signal_asof.unique().astype(int))
    rows = []
    for fold in PurgedWalkForward(split).split(dates, panel_dates):
        selected = frame[frame.signal_asof.isin(
            dates[fold.test_indices])][["signal_asof", "symbol"]].copy()
        selected["fold"] = int(fold.fold)
        rows.append(selected)
    if not rows:
        raise MLContractError("combiner has no expected OOS keys")
    return pd.concat(rows, ignore_index=True).sort_values(
        ["signal_asof", "symbol"], kind="mergesort").reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=
                        ROOT/"configs/selection/alpha158_all_mean_ml_combiner_v1.json")
    parser.add_argument("--family-root", type=Path, default=DEFAULT_FAMILY_ROOT)
    parser.add_argument("--dataset-report", type=Path,
                        default=DEFAULT_DATASET_REPORT)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research_2015_v1"))
    parser.add_argument("--source-config", type=Path, default=
                        ROOT/"configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--policy-config", type=Path, default=
                        ROOT/"configs/selection/alpha158_lite_low_turnover_v3.json")
    parser.add_argument("--risk-config", type=Path, default=
                        ROOT/"configs/selection/risk_v1.json")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--predictions-only", action="store_true")
    args = parser.parse_args()
    config = load_experiment_config(args.config)
    audit = audit_family_set(args.family_root, args.dataset_report)
    if audit["status"] != "PASS":
        raise MLContractError("M7 is incomplete: {}".format(
            ", ".join(audit["incomplete_families"])))
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    args.output_dir.mkdir(parents=True)
    matrix = load_family_matrix(args.family_root, audit)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20110101,
        end_date=20260930)
    expected = expected_combiner_keys(matrix, panel.dates, config)
    expected_hash = prediction_key_hash(
        expected, config["candidate_arm_ids"])
    registration = ExperimentRegistration(
        experiment_id=config["experiment_id"],
        experiment_family_id=config["experiment_family_id"],
        experiment_type=config["experiment_type"],
        baseline_arm_id=config["baseline_arm_id"],
        candidate_arm_ids=tuple(config["candidate_arm_ids"]),
        config_sha256=canonical_config_sha256(config),
        data_manifest_sha256=canonical_config_sha256(audit),
        expected_prediction_keys_sha256=expected_hash,
        protected_file_hashes=tuple(),
        created_at=datetime.now().astimezone().isoformat())
    write_json(args.output_dir/"registration.json", {
        "registration": registration.payload(),
        "common_evaluation_start": int(expected.signal_asof.min()),
        "common_evaluation_end": int(expected.signal_asof.max()),
        "initial_state": "equal_cash_empty_positions",
        "m7_common_key_sha256": audit["common_key_sha256"],
        "observed_history": True, "new_holdout": False,
    })
    predictions = generate_combiner_predictions(
        matrix, panel.dates, config, args.output_dir)
    if not predictions[["signal_asof", "symbol", "fold"]].equals(
            expected[["signal_asof", "symbol", "fold"]]):
        raise MLContractError("actual combiner keys differ from registration")
    if args.predictions_only:
        return
    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    results, audits = run_accounts(
        panel, predictions, source, policy, risk, args.output_dir,
        arm_columns=ARM_COLUMNS)
    evidence = evidence_for("all_mean_rank_a1", registration, results, audits)
    decision = decide_historical_screen(
        registration, "all_mean_rank_a1", evidence,
        minimum_annualized_uplift_pp=
            config["decision_gates"]["minimum_annualized_uplift_pp"],
        top5_share_max=
            config["decision_gates"]["top5_positive_profit_share_max"])
    write_json(args.output_dir/"evidence.json", evidence)
    write_json(args.output_dir/"report.json", {
        "registration": registration.payload(), "results": results,
        "decision": asdict(decision), "historical_evidence_only": True,
        "automatic_strategy_replacement": False,
    })
    write_json(args.output_dir/"completion.json", {
        "status": "COMPLETE", "decision": decision.state,
        "report_sha256": file_sha256(args.output_dir/"report.json")})


if __name__ == "__main__":
    main()
