#!/usr/bin/env python3
"""Compare one preregistered shallow nonlinear ranker with frozen Ridge."""
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
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview  # noqa: E402
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuWalkForward import (  # noqa: E402
    PurgedWalkForward, WalkForwardConfig,
)
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402
from scripts.research_alpha158_lite_v1 import (  # noqa: E402
    daily_rank_ic, daily_selection_uplift, moving_block_mean_interval,
)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")


def fit_nonlinear(frame, model_config):
    from sklearn.ensemble import HistGradientBoostingRegressor

    rows = frame.dropna(subset=["target_rank"])
    if len(rows) < 1000:
        raise ValueError("insufficient nonlinear training rows")
    date_count = rows.groupby("signal_asof").signal_asof.transform("size")
    weights = len(rows) / rows.signal_asof.nunique() / date_count
    parameters = dict(model_config)
    parameters.pop("type", None)
    model = HistGradientBoostingRegressor(**parameters)
    model.fit(
        rows[list(ALPHA158_LITE_FEATURES)], rows.target_rank,
        sample_weight=weights.to_numpy(dtype=float),
    )
    return model, {
        "model": "native_nan_hist_gradient_boosting_regressor",
        "parameters": parameters,
        "train_rows": int(len(rows)),
        "train_dates": int(rows.signal_asof.nunique()),
        "sample_weighting": "equal_total_weight_per_signal_date",
    }


def generate_predictions(panel, source, research, output):
    walk = research["walk_forward"]
    split_config = WalkForwardConfig(
        minimum_train_dates=int(walk["minimum_train_dates"]),
        validation_dates=int(walk["validation_dates"]),
        test_dates=int(walk["test_dates"]),
        label_horizon_sessions=int(walk["label_horizon_sessions"]),
        embargo_sessions=int(walk["embargo_sessions"]),
    )
    engine = Alpha158LiteFeatureEngine(panel, source)
    all_positions = np.arange(len(panel.dates))
    signal_days = np.flatnonzero(
        (panel.dates >= int(walk["start_date"])) &
        (panel.dates <= int(walk["end_date"])) &
        (all_positions >= source.minimum_history_sessions))
    snapshots = {}
    predictions, manifests = [], []
    for fold in PurgedWalkForward(split_config).split(
            panel.dates[signal_days], panel.dates):
        train_days = signal_days[fold.train_indices][
            ::int(walk["training_date_stride"])]
        frames = []
        for day in train_days:
            key = int(day)
            if key not in snapshots:
                snapshots[key] = engine.snapshot(key, include_labels=True)
            frames.append(snapshots[key])
        training = pd.concat(frames, ignore_index=True)
        ridge = Alpha158LiteModel(source).fit(
            training, panel.dates[train_days])
        nonlinear, nonlinear_manifest = fit_nonlinear(
            training, research["model"])
        manifest = {
            "fold": int(fold.fold),
            "train_start": int(fold.train_start),
            "train_end": int(fold.train_end),
            "test_start": int(fold.test_start),
            "test_end": int(fold.test_end),
            "ridge": ridge.manifest,
            "nonlinear": nonlinear_manifest,
        }
        manifests.append(manifest)
        for position in fold.test_indices:
            day = int(signal_days[position])
            snapshot = engine.snapshot(day, include_labels=True)
            if snapshot.empty:
                continue
            values = snapshot[[
                "signal_asof", "symbol", "column", "excess_return_20d",
                "target_rank",
            ]].copy()
            values["ridge_score"] = ridge.predict(snapshot)
            values["nonlinear_score"] = nonlinear.predict(
                snapshot[list(ALPHA158_LITE_FEATURES)])
            values["fold"] = int(fold.fold)
            values["train_end"] = int(fold.train_end)
            predictions.append(values)
        print(json.dumps({
            "fold": int(fold.fold), "train_dates": int(len(train_days)),
            "train_rows": int(len(training)),
            "test_start": int(fold.test_start),
            "test_end": int(fold.test_end),
        }), flush=True)
    if not predictions:
        raise RuntimeError("walk-forward produced no predictions")
    result = pd.concat(predictions, ignore_index=True)
    if result.duplicated(["signal_asof", "symbol"]).any():
        raise AssertionError("walk-forward test folds overlap")
    write_json(output / "fold_manifests.json", manifests)
    return result, engine, split_config


def year_metrics(frame, score):
    daily = daily_rank_ic(frame, score)
    return daily.assign(year=daily.signal_asof // 10000).groupby(
        "year", sort=True).agg(
            dates=("ic", "size"), mean_ic=("ic", "mean"),
            positive_ic_rate=("ic", lambda value: float((value > 0).mean())),
        ).reset_index()


def factor_report(predictions, gates):
    ridge_ic = daily_rank_ic(predictions, "ridge_score").rename(
        columns={"ic": "ridge_ic"})
    nonlinear_ic = daily_rank_ic(predictions, "nonlinear_score").rename(
        columns={"ic": "nonlinear_ic"})
    paired = ridge_ic.merge(nonlinear_ic, on="signal_asof", validate="one_to_one")
    paired["ic_delta"] = paired.nonlinear_ic - paired.ridge_ic
    delta_ci = moving_block_mean_interval(
        paired.ic_delta, block_length=20, replicates=2000, seed=20261005)
    ridge_uplift = daily_selection_uplift(
        predictions, "ridge_score", 10).rename(columns={"uplift": "ridge_uplift"})
    nonlinear_uplift = daily_selection_uplift(
        predictions, "nonlinear_score", 10).rename(
            columns={"uplift": "nonlinear_uplift"})
    paired_uplift = ridge_uplift[["signal_asof", "ridge_uplift"]].merge(
        nonlinear_uplift[["signal_asof", "nonlinear_uplift"]],
        on="signal_asof", validate="one_to_one")
    paired_uplift["uplift_delta"] = (
        paired_uplift.nonlinear_uplift - paired_uplift.ridge_uplift)
    ridge_year = year_metrics(predictions, "ridge_score").rename(
        columns={"mean_ic": "ridge_mean_ic",
                 "positive_ic_rate": "ridge_positive_ic_rate"})
    nonlinear_year = year_metrics(predictions, "nonlinear_score").rename(
        columns={"mean_ic": "nonlinear_mean_ic",
                 "positive_ic_rate": "nonlinear_positive_ic_rate"})
    yearly = ridge_year.merge(
        nonlinear_year.drop(columns=["dates"]), on="year", validate="one_to_one")
    yearly["ic_delta"] = yearly.nonlinear_mean_ic - yearly.ridge_mean_ic
    positive_years = int((yearly.ic_delta > 0).sum())
    checks = {
        "paired_ic_delta_block_ci": [float(delta_ci[0]), float(delta_ci[1])],
        "paired_ic_delta_mean": float(paired.ic_delta.mean()),
        "top10_uplift_delta_mean": float(paired_uplift.uplift_delta.mean()),
        "positive_year_ic_delta_count": positive_years,
    }
    passed = (
        checks["paired_ic_delta_block_ci"][0] >=
        float(gates["paired_ic_delta_block_ci_low_min"]) and
        checks["top10_uplift_delta_mean"] >
        float(gates["top10_uplift_delta_min"]) and
        positive_years >= int(gates["positive_year_ic_delta_min_count"])
    )
    summary = {
        "ridge_rank_ic_mean": float(paired.ridge_ic.mean()),
        "nonlinear_rank_ic_mean": float(paired.nonlinear_ic.mean()),
        "ridge_top10_uplift_mean": float(paired_uplift.ridge_uplift.mean()),
        "nonlinear_top10_uplift_mean": float(
            paired_uplift.nonlinear_uplift.mean()),
        **checks,
        "factor_gate": "PASS" if passed else "FAIL",
    }
    return summary, paired, paired_uplift, yearly


def portfolio_backtest(panel, predictions, source, policy, risk, end_date):
    results, annual = {}, {}
    for model in ("ridge", "nonlinear"):
        column = model + "_score"
        frame = predictions[[
            "signal_asof", "symbol", "column", column]].rename(
                columns={column: "alpha_score"})
        scores = rank_frame(frame, "alpha_score", max(
            policy.entry_rank_limit, policy.retention_rank_limit))
        result, audit = run_low_turnover(
            panel, scores, source, policy, risk, end_date,
            review_overlay=CostAwareReview(suppress_rank_exits=True))
        results[model] = result
        annual[model] = annual_returns(audit["curve"]).to_dict("records")
    return results, annual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_ranker_nonlinear_research_v1.json")
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
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_ranker_nonlinear_v1_20261005"))
    args = parser.parse_args()

    research = json.loads(args.research_config.read_text(encoding="utf-8"))
    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    if source.strategy_version != research["source_strategy_version"]:
        raise ValueError("research source strategy mismatch")
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("portfolio policy source hash mismatch")
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=False)
    registration = {
        "experiment": research,
        "experiment_sha256": canonical_hash(research),
        "source_config": asdict(source),
        "source_config_sha256": source.sha256,
        "policy_config": asdict(policy),
        "policy_config_sha256": policy.sha256,
        "risk_config_sha256": risk.sha256,
        "features": list(ALPHA158_LITE_FEATURES),
        "registered_before_results": True,
    }
    write_json(output / "registration.json", registration)

    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=int(research["walk_forward"]["end_date"]))
    predictions, engine, split = generate_predictions(
        panel, source, research, output)
    factor, paired_ic, paired_uplift, yearly = factor_report(
        predictions, research["gates"])
    predictions.to_csv(
        output / "oos_predictions.csv.gz", index=False,
        compression={"method": "gzip", "compresslevel": 3})
    paired_ic.to_csv(output / "paired_daily_ic.csv", index=False)
    paired_uplift.to_csv(output / "paired_top10_uplift.csv", index=False)
    yearly.to_csv(output / "year_metrics.csv", index=False)

    portfolio, annual = portfolio_backtest(
        panel, predictions, source, policy, risk,
        int(research["walk_forward"]["end_date"]))
    ridge, nonlinear = portfolio["ridge"], portfolio["nonlinear"]
    comparison = {
        "return_delta_pct_points": float(
            nonlinear["return_pct"] - ridge["return_pct"]),
        "max_drawdown_delta_pct_points": float(
            nonlinear["max_drawdown_pct"] - ridge["max_drawdown_pct"]),
        "liquidation_return_delta_pct_points": float(
            nonlinear["liquidation_3_limits_return_pct"] -
            ridge["liquidation_3_limits_return_pct"]),
        "average_exposure_delta_pct_points": float(
            nonlinear["average_exposure_pct"] - ridge["average_exposure_pct"]),
        "filled_buy_delta": int(
            nonlinear["filled_buys"] - ridge["filled_buys"]),
    }
    gates = research["gates"]
    portfolio_pass = (
        comparison["return_delta_pct_points"] >
        float(gates["portfolio_return_delta_pct_points_min"]) and
        comparison["max_drawdown_delta_pct_points"] >=
        -float(gates["max_drawdown_worsening_limit_pct_points"]) and
        comparison["liquidation_return_delta_pct_points"] >
        float(gates["liquidation_return_delta_pct_points_min"])
    )
    decision = (
        "RETAIN_FOR_NEW_FORWARD_SHADOW_ONLY"
        if factor["factor_gate"] == "PASS" and portfolio_pass
        else "REJECT_HISTORICAL_SCREEN")
    report = {
        "experiment_id": research["experiment_id"],
        "role": research["research_status"],
        "feature_config_sha256": engine.feature_config_sha256,
        "label_config_sha256": engine.label_config_sha256,
        "split": asdict(split),
        "oos_rows": int(len(predictions)),
        "oos_dates": int(predictions.signal_asof.nunique()),
        "factor": factor,
        "portfolio": portfolio,
        "portfolio_comparison": comparison,
        "annual_returns": annual,
        "portfolio_gate": "PASS" if portfolio_pass else "FAIL",
        "decision": decision,
        "parameter_search_performed": False,
        "observed_history": True,
        "new_holdout": False,
        "warning": (
            "All history has already been observed. A pass can only justify "
            "a separately registered forward shadow, never immediate adoption."
        ),
    }
    write_json(output / "report.json", report)
    write_json(output / "completion.json", {
        "status": "COMPLETE", "decision": decision,
        "report_sha256": canonical_hash(report),
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
