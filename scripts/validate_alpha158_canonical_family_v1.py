#!/usr/bin/env python3
"""Validate one pre-registered Alpha158 canonical technical family."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Canonical import (  # noqa: E402
    ALPHA158_CANONICAL_FAMILIES, CANONICAL_WINDOWS,
    Alpha158CanonicalFeatureEngine,
)
from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    ALPHA158_LITE_FEATURES, Alpha158LiteModel,
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuWalkForward import (  # noqa: E402
    PurgedWalkForward, WalkForwardConfig,
)
from scripts.validate_alpha158_ranker_nonlinear_v1 import (  # noqa: E402
    canonical_hash, write_json,
)
from scripts.validate_alpha158_residual_overheat_v1 import (  # noqa: E402
    evaluate_factor, run_portfolios,
)


def fit_candidate(frame, source, features):
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import Pipeline

    rows = frame.dropna(subset=["target_rank"])
    pipeline = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("model", Ridge(alpha=source.ridge_alpha)),
    ])
    date_count = rows.groupby("signal_asof").signal_asof.transform("size")
    weights = len(rows) / rows.signal_asof.nunique() / date_count
    pipeline.fit(
        rows[list(features)], rows.target_rank,
        model__sample_weight=weights.to_numpy(dtype=float))
    return pipeline, {
        "model": "median_imputer_plus_ridge",
        "ridge_alpha": float(source.ridge_alpha),
        "features": list(features),
        "train_rows": int(len(rows)),
        "train_dates": int(rows.signal_asof.nunique()),
        "sample_weighting": "equal_total_weight_per_signal_date",
    }


def generate_predictions(panel, source, research, family, output):
    walk = research["walk_forward"]
    split = WalkForwardConfig(
        minimum_train_dates=int(walk["minimum_train_dates"]),
        validation_dates=int(walk["validation_dates"]),
        test_dates=int(walk["test_dates"]),
        label_horizon_sessions=int(walk["label_horizon_sessions"]),
        embargo_sessions=int(walk["embargo_sessions"]),
    )
    engine = Alpha158CanonicalFeatureEngine(panel, source, family)
    candidate_features = (*ALPHA158_LITE_FEATURES, *engine.feature_names)
    positions = pd.RangeIndex(len(panel.dates)).to_numpy()
    signal_days = positions[
        (panel.dates >= int(walk["start_date"])) &
        (panel.dates <= int(walk["end_date"])) &
        (positions >= source.minimum_history_sessions)]
    predictions, manifests, training_cache = [], [], {}
    for fold in PurgedWalkForward(split).split(
            panel.dates[signal_days], panel.dates):
        train_days = signal_days[fold.train_indices][
            ::int(walk["training_date_stride"])]
        training_frames = []
        for day in train_days:
            key = int(day)
            if key not in training_cache:
                training_cache[key] = engine.snapshot(key)
            training_frames.append(training_cache[key])
        training = pd.concat(training_frames, ignore_index=True)
        baseline = Alpha158LiteModel(source).fit(
            training, panel.dates[train_days])
        candidate, candidate_manifest = fit_candidate(
            training, source, candidate_features)
        manifests.append({
            "fold": int(fold.fold), "train_start": int(fold.train_start),
            "train_end": int(fold.train_end),
            "validation_start": int(fold.validation_start),
            "validation_end": int(fold.validation_end),
            "test_start": int(fold.test_start), "test_end": int(fold.test_end),
            "label_horizon_sessions": int(fold.label_horizon_sessions),
            "additional_embargo_sessions": int(
                fold.additional_embargo_sessions),
            "train_label_end": int(fold.train_label_end),
            "validation_label_end": int(fold.validation_label_end),
            "train_to_validation_clear_sessions": int(
                fold.train_to_validation_clear_sessions),
            "validation_to_test_clear_sessions": int(
                fold.validation_to_test_clear_sessions),
            "strict_label_non_overlap": True,
            "baseline": baseline.manifest, "candidate": candidate_manifest,
        })
        for position in fold.test_indices:
            day = int(signal_days[position])
            snapshot = engine.snapshot(day)
            if snapshot.empty:
                continue
            values = snapshot[[
                "signal_asof", "symbol", "column", "excess_return_20d",
                "target_rank",
            ]].copy()
            values["ridge_score"] = baseline.predict(snapshot)
            values["candidate_score"] = candidate.predict(
                snapshot[list(candidate_features)])
            values["fold"] = int(fold.fold)
            values["train_end"] = int(fold.train_end)
            predictions.append(values)
        print(json.dumps({
            "family": family, "fold": int(fold.fold),
            "train_dates": int(len(train_days)),
            "train_rows": int(len(training)),
            "test_start": int(fold.test_start),
            "test_end": int(fold.test_end),
        }), flush=True)
    result = pd.concat(predictions, ignore_index=True)
    if result.duplicated(["signal_asof", "symbol"]).any():
        raise AssertionError("walk-forward test folds overlap")
    write_json(output / "fold_manifests.json", manifests)
    return result, engine, split


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", required=True,
                        choices=tuple(ALPHA158_CANONICAL_FAMILIES))
    parser.add_argument("--research-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_canonical_families_research_v1.json")
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
    parser.add_argument("--vwap-audit-report", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_vwap_scale_audit_v1_20261006/report.json"))
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    research = json.loads(args.research_config.read_text(encoding="utf-8"))
    if args.family not in research["families"]:
        raise ValueError("family is not pre-registered")
    if tuple(research["canonical_windows"]) != CANONICAL_WINDOWS:
        raise ValueError("canonical windows differ from pre-registration")
    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    if source.strategy_version != research["source_strategy_version"]:
        raise ValueError("source strategy mismatch")
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("policy source hash mismatch")
    vwap_audit = None
    if args.family == "vwap_price":
        gate = research.get("vwap_data_gate")
        if gate is None:
            raise ValueError("VWAP family requires a registered data gate")
        if not args.vwap_audit_report.exists():
            raise FileNotFoundError("VWAP scale audit report is missing")
        vwap_audit = json.loads(args.vwap_audit_report.read_text(
            encoding="utf-8"))
        if vwap_audit.get("audit_id") != gate["audit_id"] or \
                vwap_audit.get("decision") != gate["decision_required"]:
            raise ValueError("VWAP scale audit did not satisfy the data gate")

    output = args.output_dir or Path(
        "/Users/wjy/abu/backtests/alpha158_canonical_{}_v1_20261006".format(
            args.family))
    output.mkdir(parents=True, exist_ok=False)
    family_features = ALPHA158_CANONICAL_FAMILIES[args.family]
    write_json(output / "registration.json", {
        "experiment": research,
        "experiment_sha256": canonical_hash(research),
        "family": args.family,
        "source_config": asdict(source),
        "source_config_sha256": source.sha256,
        "policy_config": asdict(policy),
        "policy_config_sha256": policy.sha256,
        "risk_config_sha256": risk.sha256,
        "baseline_features": list(ALPHA158_LITE_FEATURES),
        "candidate_features": [*ALPHA158_LITE_FEATURES, *family_features],
        "vwap_data_gate": None if vwap_audit is None else {
            "path": str(args.vwap_audit_report),
            "audit_id": vwap_audit["audit_id"],
            "decision": vwap_audit["decision"],
            "report_sha256": canonical_hash(vwap_audit),
        },
        "registered_before_results": True,
    })

    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=int(research["walk_forward"]["end_date"]))
    predictions, engine, split = generate_predictions(
        panel, source, research, args.family, output)
    factor, paired, uplift, yearly = evaluate_factor(
        predictions, research["gates"])
    predictions.to_csv(
        output / "oos_predictions.csv.gz", index=False,
        compression={"method": "gzip", "compresslevel": 3})
    paired.to_csv(output / "paired_daily_ic.csv", index=False)
    uplift.to_csv(output / "paired_top10_uplift.csv", index=False)
    yearly.to_csv(output / "year_metrics.csv", index=False)

    portfolio, annual = run_portfolios(
        panel, predictions, source, policy, risk,
        int(research["walk_forward"]["end_date"]))
    baseline, candidate = portfolio["ridge"], portfolio["candidate"]
    comparison = {
        "return_delta_pct_points": float(
            candidate["return_pct"] - baseline["return_pct"]),
        "max_drawdown_delta_pct_points": float(
            candidate["max_drawdown_pct"] - baseline["max_drawdown_pct"]),
        "liquidation_return_delta_pct_points": float(
            candidate["liquidation_3_limits_return_pct"] -
            baseline["liquidation_3_limits_return_pct"]),
        "average_exposure_delta_pct_points": float(
            candidate["average_exposure_pct"] -
            baseline["average_exposure_pct"]),
        "filled_buy_delta": int(
            candidate["filled_buys"] - baseline["filled_buys"]),
    }
    gates = research["gates"]
    portfolio_pass = (
        comparison["return_delta_pct_points"] >
        float(gates["portfolio_return_delta_pct_points_min"]) and
        comparison["max_drawdown_delta_pct_points"] >=
        -float(gates["max_drawdown_worsening_limit_pct_points"]) and
        comparison["liquidation_return_delta_pct_points"] >
        float(gates["liquidation_return_delta_pct_points_min"]))
    decision = (
        "RETAIN_FOR_NEW_FORWARD_SHADOW_ONLY"
        if factor["factor_gate"] == "PASS" and portfolio_pass
        else "REJECT_HISTORICAL_SCREEN")
    report = {
        "experiment_id": research["experiment_id"],
        "family": args.family,
        "role": research["research_status"],
        "base_feature_config_sha256": engine.base.feature_config_sha256,
        "label_config_sha256": engine.base.label_config_sha256,
        "split": asdict(split),
        "feature_count_added": len(family_features),
        "features_added": list(family_features),
        "oos_rows": int(len(predictions)),
        "oos_dates": int(predictions.signal_asof.nunique()),
        "factor": factor, "portfolio": portfolio,
        "portfolio_comparison": comparison, "annual_returns": annual,
        "portfolio_gate": "PASS" if portfolio_pass else "FAIL",
        "decision": decision,
        "parameter_search_performed": False,
        "observed_history": True, "new_holdout": False,
        "warning": (
            "Observed history only. A pass may justify a separately "
            "registered forward shadow but never immediate adoption."),
    }
    write_json(output / "report.json", report)
    write_json(output / "completion.json", {
        "status": "COMPLETE", "decision": decision,
        "report_sha256": canonical_hash(report),
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
