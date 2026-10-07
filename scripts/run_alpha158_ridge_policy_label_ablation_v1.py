#!/usr/bin/env python3
"""Run a strict Ridge ablation: excess-20 target versus event-exit target."""
from __future__ import annotations

import argparse
import hashlib
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
    ALPHA158_LITE_FEATURES, Alpha158LiteFeatureEngine, Alpha158LiteModel,
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuAlpha158PolicyLabels import (  # noqa: E402
    Alpha158PolicyLabelBuilder, load_alpha158_policy_label_config,
)
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuWalkForward import PurgedWalkForward, WalkForwardConfig  # noqa: E402
from abupy.MLBu.ABuMLContracts import (  # noqa: E402
    ExperimentRegistration, canonical_config_sha256,
)
from abupy.MLBu.ABuMLDecision import decide_historical_screen  # noqa: E402
from scripts.run_alpha158_ml_score_replacement_v1 import (  # noqa: E402
    evidence_for, prediction_key_hash, run_accounts, write_json,
)


DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_ridge_policy_label_ablation_v1_20261007")
ARM_COLUMNS = {
    "ridge_excess20_common_v1": "excess20_ridge_score",
    "ridge_event_r60_v1": "event_r60_ridge_score",
}


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def common_training_rows(frame):
    """Freeze identical rows for both arms; only the target value may differ."""
    required = {"target_rank", "event_target_rank", *ALPHA158_LITE_FEATURES}
    missing = required-set(frame.columns)
    if missing:
        raise ValueError("training frame missing: {}".format(
            ", ".join(sorted(missing))))
    valid = frame.target_rank.notna() & frame.event_target_rank.notna()
    result = frame.loc[valid].copy()
    if result.empty:
        raise ValueError("common label training rows are empty")
    return result


class SnapshotProvider(object):
    """Build PIT features once and attach fixed-maturity event labels."""

    def __init__(self, panel, source, label_config):
        self.panel = panel
        self.engine = Alpha158LiteFeatureEngine(panel, source)
        self.labels = Alpha158PolicyLabelBuilder(panel, source, label_config)
        self.cache = {}

    def _build(self, day):
        day = int(day)
        snapshot = self.engine.snapshot(day, include_labels=True)
        snapshot["event_target_rank"] = np.nan
        snapshot["event_path_r_60d"] = np.nan
        snapshot["event_label_fully_mature_date"] = np.nan
        if snapshot.empty or day+61 >= len(self.panel.dates):
            return snapshot
        labels = self.labels.build_day(
            day, snapshot.column.to_numpy(dtype=int)).set_index("column")
        ordered = labels.reindex(snapshot.column.to_numpy(dtype=int))
        if ordered.index.has_duplicates or ordered.signal_asof.isna().any():
            raise ValueError("event label keys differ from feature snapshot")
        snapshot["event_target_rank"] = ordered.event_path_r_60d.rank(
            method="average", pct=True).to_numpy(dtype=float)-.5
        snapshot["event_path_r_60d"] = \
            ordered.event_path_r_60d.to_numpy(dtype=float)
        snapshot["event_label_fully_mature_date"] = \
            ordered.label_fully_mature_date.to_numpy(dtype=float)
        return snapshot

    def get(self, day, cache=False):
        day = int(day)
        if day in self.cache:
            return self.cache[day]
        snapshot = self._build(day)
        if cache:
            self.cache[day] = snapshot
        return snapshot


def _fit_pair(training, source, train_dates):
    common = common_training_rows(training)
    baseline = Alpha158LiteModel(source).fit(common, train_dates)
    event_training = common.copy()
    event_training["target_rank"] = event_training.event_target_rank
    candidate = Alpha158LiteModel(source).fit(event_training, train_dates)
    if baseline.manifest["train_rows"] != candidate.manifest["train_rows"]:
        raise AssertionError("label arms used different training rows")
    return baseline, candidate, common


def _fold_config(config):
    walk = config["walk_forward"]
    return WalkForwardConfig(
        minimum_train_dates=int(walk["minimum_train_dates"]),
        validation_dates=int(walk["validation_dates"]),
        test_dates=int(walk["test_dates"]),
        label_horizon_sessions=int(walk["label_horizon_sessions"]),
        embargo_sessions=int(walk["embargo_sessions"]))


def generate_predictions(panel, source, label_config, config, output):
    walk = config["walk_forward"]
    positions = np.arange(len(panel.dates))
    signal_days = np.flatnonzero(
        (panel.dates >= int(walk["signal_start"])) &
        (panel.dates <= int(walk["signal_end"])) &
        (positions >= source.minimum_history_sessions))
    folds = list(PurgedWalkForward(_fold_config(config)).split(
        panel.dates[signal_days], panel.dates))
    if not folds:
        raise RuntimeError("61-session purged walk-forward produced no folds")
    train_step = int(walk["train_date_step"])
    cached_days = {
        int(day) for fold in folds
        for day in signal_days[fold.train_indices][::train_step]
    }
    provider = SnapshotProvider(panel, source, label_config)
    fold_root = output/"prediction_folds"
    fold_root.mkdir(exist_ok=True)
    manifests = []
    for fold in folds:
        fold_id = int(fold.fold)
        prediction_path = fold_root/"fold_{:02d}.csv.gz".format(fold_id)
        manifest_path = fold_root/"fold_{:02d}.json".format(fold_id)
        if prediction_path.exists() and manifest_path.exists():
            manifests.append(json.loads(manifest_path.read_text(
                encoding="utf-8")))
            print(json.dumps({"fold": fold_id, "status": "REUSED"}), flush=True)
            continue
        train_days = signal_days[fold.train_indices][::train_step]
        training = pd.concat([
            provider.get(int(day), cache=True) for day in train_days
        ], ignore_index=True)
        baseline, candidate, common = _fit_pair(
            training, source, panel.dates[train_days])
        maximum_maturity = int(np.nanmax(
            common.event_label_fully_mature_date.to_numpy(dtype=float)))
        if maximum_maturity >= int(fold.validation_start):
            raise AssertionError("event training label overlaps validation")
        rows = []
        for position in fold.test_indices:
            day = int(signal_days[position])
            snapshot = provider.get(day, cache=day in cached_days)
            values = snapshot[[
                "signal_asof", "symbol", "column", "target_rank",
                "event_target_rank", "event_path_r_60d",
                "event_label_fully_mature_date"]].copy()
            values["excess20_ridge_score"] = baseline.predict(snapshot)
            values["event_r60_ridge_score"] = candidate.predict(snapshot)
            values["fold"] = fold_id
            rows.append(values)
        predictions = pd.concat(rows, ignore_index=True).sort_values(
            ["signal_asof", "symbol"], kind="mergesort").reset_index(drop=True)
        if predictions.duplicated(["signal_asof", "symbol"]).any():
            raise AssertionError("fold contains duplicate prediction keys")
        predictions.to_csv(
            prediction_path, index=False,
            compression={"method": "gzip", "compresslevel": 3, "mtime": 0})
        manifest = {
            "fold": fold_id,
            "train_start": int(fold.train_start),
            "train_end": int(fold.train_end),
            "train_label_end": int(fold.train_label_end),
            "maximum_observed_event_label_maturity": maximum_maturity,
            "validation_start": int(fold.validation_start),
            "validation_end": int(fold.validation_end),
            "validation_label_end": int(fold.validation_label_end),
            "test_start": int(fold.test_start),
            "test_end": int(fold.test_end),
            "common_train_rows": int(len(common)),
            "prediction_rows": int(len(predictions)),
            "baseline_model": baseline.manifest,
            "candidate_model": candidate.manifest,
            "targets_only_difference": True,
            "prediction_sha256": file_sha256(prediction_path),
        }
        write_json(manifest_path, manifest)
        manifests.append(manifest)
        print(json.dumps({
            "fold": fold_id, "status": "COMPLETE",
            "train_rows": len(common), "prediction_rows": len(predictions),
            "test_start": int(fold.test_start), "test_end": int(fold.test_end),
        }), flush=True)
    combined = pd.concat([
        pd.read_csv(fold_root/"fold_{:02d}.csv.gz".format(int(fold.fold)),
                    dtype={"symbol": str}) for fold in folds
    ], ignore_index=True).sort_values(
        ["signal_asof", "symbol"], kind="mergesort").reset_index(drop=True)
    if combined.duplicated(["signal_asof", "symbol"]).any():
        raise AssertionError("OOS prediction folds overlap")
    combined.to_csv(
        output/"oos_predictions.csv.gz", index=False,
        compression={"method": "gzip", "compresslevel": 3, "mtime": 0})
    write_json(output/"fold_manifests.json", manifests)
    return combined


def _safe_daily_ic(frame, score, target):
    rows = []
    for date, group in frame.groupby("signal_asof", sort=True):
        valid = group[[score, target]].dropna()
        value = np.nan
        if len(valid) >= 3 and valid[score].nunique() > 1 and \
                valid[target].nunique() > 1:
            value = float(valid[score].corr(valid[target]))
        rows.append({"signal_asof": int(date), "ic": value})
    return pd.DataFrame(rows)


def factor_diagnostics(predictions):
    result = {}
    for arm, score in ARM_COLUMNS.items():
        result[arm] = {}
        for target in ("target_rank", "event_target_rank"):
            daily = _safe_daily_ic(predictions, score, target)
            valid = daily.ic.dropna()
            result[arm][target] = {
                "dates": int(len(valid)),
                "mean_ic": float(valid.mean()),
                "median_ic": float(valid.median()),
                "positive_date_rate": float(valid.gt(0).mean()),
            }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-config", type=Path, default=
                        ROOT/"configs/selection/alpha158_ridge_policy_label_ablation_v1.json")
    parser.add_argument("--source-config", type=Path, default=
                        ROOT/"configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--label-config", type=Path, default=
                        ROOT/"configs/selection/alpha158_policy_labels_v1.json")
    parser.add_argument("--policy-config", type=Path, default=
                        ROOT/"configs/selection/alpha158_lite_low_turnover_v3.json")
    parser.add_argument("--risk-config", type=Path, default=
                        ROOT/"configs/selection/risk_v1.json")
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research_2015_v1"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--predictions-only", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.experiment_config.read_text(encoding="utf-8"))
    preregistration_path = args.output_dir/"preregistration.json"
    if args.output_dir.exists() and not preregistration_path.exists():
        raise FileExistsError(args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    config_sha256 = canonical_config_sha256(config)
    if preregistration_path.exists():
        preregistered = json.loads(preregistration_path.read_text(
            encoding="utf-8"))
        if preregistered["config_sha256"] != config_sha256:
            raise RuntimeError("resumed experiment config differs")
    else:
        write_json(preregistration_path, {
            "experiment_id": config["experiment_id"],
            "config_sha256": config_sha256,
            "baseline_target": config["baseline_label_id"],
            "candidate_target": config["candidate_label_id"],
            "common_training_rows": True,
            "purge_sessions": int(
                config["walk_forward"]["label_horizon_sessions"]),
            "registered_at": datetime.now().astimezone().isoformat(),
            "historical_evidence_only": True,
        })
    source = load_alpha158_lite_config(args.source_config)
    label_config = load_alpha158_policy_label_config(args.label_config)
    if float(config["ridge_alpha"]) != source.ridge_alpha:
        raise ValueError("experiment and source Ridge alpha differ")
    if float(config["event_label_cost_bps_per_side"]) != \
            label_config.entry_slippage_bps:
        raise ValueError("event label entry cost differs from registration")
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20110101,
        end_date=int(config["walk_forward"]["signal_end"]))
    predictions = generate_predictions(
        panel, source, label_config, config, args.output_dir)
    registration = ExperimentRegistration(
        experiment_id=config["experiment_id"],
        experiment_family_id=config["experiment_family_id"],
        experiment_type=config["experiment_type"],
        baseline_arm_id=config["baseline_arm_id"],
        candidate_arm_ids=(config["candidate_arm_id"],),
        config_sha256=config_sha256,
        data_manifest_sha256=file_sha256(
            args.output_dir/"oos_predictions.csv.gz"),
        expected_prediction_keys_sha256=prediction_key_hash(
            predictions, [config["candidate_arm_id"]]),
        protected_file_hashes=tuple(),
        created_at=datetime.now().astimezone().isoformat())
    write_json(args.output_dir/"registration.json", {
        "registration": registration.payload(),
        "baseline_target": config["baseline_label_id"],
        "candidate_target": config["candidate_label_id"],
        "common_training_rows": True,
        "purge_sessions": int(config["walk_forward"]["label_horizon_sessions"]),
        "observed_history": True, "new_holdout": False,
    })
    diagnostics = factor_diagnostics(predictions)
    write_json(args.output_dir/"factor_diagnostics.json", diagnostics)
    if args.predictions_only:
        return
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    results, audits = run_accounts(
        panel, predictions, source, policy, risk, args.output_dir,
        arm_columns=ARM_COLUMNS)
    evidence = evidence_for(
        config["candidate_arm_id"], registration, results, audits)
    decision = decide_historical_screen(
        registration, config["candidate_arm_id"], evidence,
        minimum_annualized_uplift_pp=
            config["decision_gates"]["minimum_annualized_uplift_pp"],
        top5_share_max=
            config["decision_gates"]["top5_positive_profit_share_max"])
    write_json(args.output_dir/"evidence.json", evidence)
    write_json(args.output_dir/"report.json", {
        "registration": registration.payload(),
        "factor_diagnostics": diagnostics,
        "results": results,
        "evidence": evidence,
        "decision": asdict(decision),
        "historical_evidence_only": True,
        "automatic_strategy_replacement": False,
    })
    write_json(args.output_dir/"completion.json", {
        "status": "COMPLETE", "decision": decision.state,
        "report_sha256": file_sha256(args.output_dir/"report.json")})


if __name__ == "__main__":
    main()
