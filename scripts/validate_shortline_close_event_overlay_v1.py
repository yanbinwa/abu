#!/usr/bin/env python3
"""Retrospective close-event overlay on strict Alpha158 OOS predictions."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import replace
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
from abupy.AlphaBu.ABuShortLineCloseFactors import (  # noqa: E402
    attach_close_event_features, build_close_event_features,
    load_complete_history,
)
from abupy.AlphaBu.ABuWalkForward import (  # noqa: E402
    PurgedWalkForward, WalkForwardConfig,
)
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import (  # noqa: E402
    _save_audit, rank_frame,
)


DEFAULT_CONFIG = (
    ROOT / "configs/selection/shortline_close_event_overlay_v1.json")
DEFAULT_HISTORY = Path(
    "/Users/wjy/abu/data/selection_research/eltdx_close_history_v1")
DEFAULT_PREDICTIONS = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_canonical_price_volume_persistence_v2_20261006/"
    "oos_predictions.csv.gz")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, payload):
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def validate_config(config):
    if config["research_status"] != "RETROSPECTIVE_SCREEN_ONLY_NOT_ADMITTED":
        raise ValueError("event overlay must remain retrospective research")
    if config["strict_pit"] is not False or \
            config["availability_evidence"] != "BACKFILLED_QUERY":
        raise ValueError("historical eltdx input cannot be declared strict PIT")
    if config["automatic_admission"] is not False:
        raise ValueError("historical event overlay cannot auto-admit")
    if int(config["label_horizon_sessions"]) != 20:
        raise ValueError("experiment is frozen to a 20-session label")
    if config["model"] != "median_imputer_standard_scaler_ridge":
        raise ValueError("unregistered model")
    arms = config["factor_arms"]
    if set(arms) != {"meta_base", "meta_stock", "meta_market",
                     "meta_combined"}:
        raise ValueError("factor arms differ from frozen experiment")
    forbidden = set(config["excluded_from_primary"])
    if any(forbidden & set(features) for features in arms.values()):
        raise ValueError("revision-prone fields entered primary features")


def fit_model(frame, features, alpha):
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    clean = frame.dropna(subset=["target_rank"])
    if clean.empty:
        raise ValueError("empty meta-model training frame")
    pipeline = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("model", Ridge(alpha=float(alpha))),
    ])
    date_size = clean.groupby("signal_asof").signal_asof.transform("size")
    weights = len(clean) / clean.signal_asof.nunique() / date_size
    pipeline.fit(clean[list(features)], clean.target_rank,
                 model__sample_weight=weights.to_numpy(dtype=float))
    return pipeline


def generate_meta_predictions(frame, calendar_dates, config):
    dates = np.asarray(sorted(frame.signal_asof.astype(int).unique()), dtype=int)
    walk = config["walk_forward"]
    split = WalkForwardConfig(
        minimum_train_dates=int(walk["minimum_train_dates"]),
        validation_dates=int(walk["validation_dates"]),
        test_dates=int(walk["test_dates"]),
        label_horizon_sessions=int(config["label_horizon_sessions"]),
        embargo_sessions=int(walk["embargo_sessions"]),
    )
    by_date = {int(date): group for date, group in
               frame.groupby("signal_asof", sort=True)}
    predictions, manifests = [], []
    for fold in PurgedWalkForward(split).split(dates, calendar_dates):
        train_dates = dates[fold.train_indices][
            ::int(walk["training_date_stride"])]
        test_dates = dates[fold.test_indices]
        training = pd.concat([by_date[int(date)] for date in train_dates],
                             ignore_index=True)
        models = {
            arm: fit_model(training, features, walk["ridge_alpha"])
            for arm, features in config["factor_arms"].items()
        }
        for date in test_dates:
            test = by_date[int(date)].copy()
            output = test[[
                "signal_asof", "symbol", "column", "ridge_score",
                "excess_return_20d", "target_rank",
            ]].copy()
            for arm, model in models.items():
                output[arm] = model.predict(
                    test[config["factor_arms"][arm]])
            output["meta_fold"] = int(fold.fold)
            output["meta_train_end"] = int(fold.train_end)
            predictions.append(output)
        manifests.append({
            "fold": int(fold.fold),
            "train_start": int(fold.train_start),
            "train_end": int(fold.train_end),
            "validation_start": int(fold.validation_start),
            "validation_end": int(fold.validation_end),
            "test_start": int(fold.test_start),
            "test_end": int(fold.test_end),
            "train_label_end": int(fold.train_label_end),
            "validation_label_end": int(fold.validation_label_end),
            "train_to_validation_clear_sessions": int(
                fold.train_to_validation_clear_sessions),
            "validation_to_test_clear_sessions": int(
                fold.validation_to_test_clear_sessions),
            "strict_label_non_overlap": True,
            "train_dates_sampled": int(len(train_dates)),
        })
    if not predictions:
        raise ValueError("insufficient complete event dates for meta walk-forward")
    result = pd.concat(predictions, ignore_index=True)
    if result.duplicated(["signal_asof", "symbol"]).any():
        raise AssertionError("meta walk-forward test rows overlap")
    return result, manifests


def daily_rank_ic(frame, score):
    rows = []
    for date, group in frame.groupby("signal_asof", sort=True):
        clean = group.dropna(subset=[score, "excess_return_20d"])
        if len(clean) < 20 or clean[score].nunique() < 2:
            continue
        value = clean[score].corr(clean.excess_return_20d, method="spearman")
        if np.isfinite(value):
            rows.append({"signal_asof": int(date), "score": score,
                         "rank_ic": float(value)})
    return pd.DataFrame(rows)


def daily_top10_uplift(frame, score):
    rows = []
    for date, group in frame.groupby("signal_asof", sort=True):
        clean = group.dropna(subset=[score, "excess_return_20d"])
        if len(clean) < 10:
            continue
        rows.append({
            "signal_asof": int(date), "score": score,
            "uplift": float(clean.nlargest(10, score).excess_return_20d.mean() -
                            clean.excess_return_20d.mean()),
        })
    return pd.DataFrame(rows)


def moving_block_means(values, block, replicates, seed):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return np.array([], dtype=float)
    block = max(1, min(int(block), len(values)))
    blocks = int(math.ceil(len(values) / block))
    offsets = np.arange(block)
    rng = np.random.default_rng(int(seed))
    output = np.empty(int(replicates), dtype=float)
    for index in range(int(replicates)):
        starts = rng.integers(0, len(values), size=blocks)
        positions = ((starts[:, None] + offsets) % len(values)).ravel()
        output[index] = values[positions[:len(values)]].mean()
    return output


def benjamini_hochberg(pvalues):
    """Return adjusted p-values in the original key order."""
    items = sorted(((key, float(value)) for key, value in pvalues.items()),
                   key=lambda item: item[1])
    adjusted = {}
    running = 1.0
    count = len(items)
    for rank in range(count, 0, -1):
        key, value = items[rank - 1]
        running = min(running, value * count / rank)
        adjusted[key] = min(1.0, running)
    return {key: adjusted[key] for key in pvalues}


def factor_report(predictions, config):
    scores = ["ridge_score", *config["factor_arms"]]
    ic = pd.concat([daily_rank_ic(predictions, score) for score in scores],
                   ignore_index=True)
    uplift = pd.concat([
        daily_top10_uplift(predictions, score) for score in scores],
        ignore_index=True)
    pivot = ic.pivot(index="signal_asof", columns="score", values="rank_ic")
    primary, pvalues = [], {}
    base = pivot["meta_base"]
    for offset, arm in enumerate(("meta_stock", "meta_market",
                                  "meta_combined")):
        delta = (pivot[arm] - base).dropna()
        samples = moving_block_means(
            delta, config["bootstrap_block_sessions"],
            config["bootstrap_replicates"], 20261006 + offset)
        pvalue = float((1 + (samples <= 0).sum()) / (len(samples) + 1))
        pvalues[arm] = pvalue
        primary.append({
            "arm": arm, "dates": int(len(delta)),
            "rank_ic_delta_mean": float(delta.mean()),
            "rank_ic_delta_ci95_low": float(np.quantile(samples, .025)),
            "rank_ic_delta_ci95_high": float(np.quantile(samples, .975)),
            "one_sided_block_pvalue": pvalue,
        })
    adjusted = benjamini_hochberg(pvalues)
    for row in primary:
        row["fdr_adjusted_pvalue"] = float(adjusted[row["arm"]])
    summary = ic.groupby("score").rank_ic.agg(
        ["count", "mean", "std"]).reset_index()
    top = uplift.groupby("score").uplift.agg(
        ["count", "mean", "std"]).reset_index()
    return ic, uplift, summary, top, primary


def run_portfolios(predictions, panel, config, source, policy, risk, output,
                   review_overlay=None):
    rows, annual_rows = [], []
    score_columns = ["ridge_score", *config["factor_arms"]]
    for cost in config["slippage_bps"]:
        for score in score_columns:
            key = "{}_{}bp".format(score, int(cost))
            values = predictions[["signal_asof", "symbol", "column", score]].rename(
                columns={score: "alpha_score"})
            ranked = rank_frame(
                values, "alpha_score",
                max(policy.entry_rank_limit, policy.retention_rank_limit))
            result, audit = run_low_turnover(
                panel, ranked,
                replace(source, label_slippage_bps=float(cost)),
                policy, risk, int(config["primary_overlap_end"]),
                review_overlay=review_overlay)
            result.update({"key": key, "score": score,
                           "slippage_bps": float(cost)})
            rows.append(result)
            yearly = annual_returns(audit["curve"])
            yearly.insert(0, "key", key)
            annual_rows.append(yearly)
            _save_audit(output / key, audit)
            print(json.dumps({
                "key": key, "return_pct": result["return_pct"],
                "max_drawdown_pct": result["max_drawdown_pct"],
                "average_exposure_pct": result["average_exposure_pct"],
            }), flush=True)
    return pd.DataFrame(rows), pd.concat(annual_rows, ignore_index=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--source-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--policy-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json")
    parser.add_argument("--risk-config", type=Path, default=
                        ROOT / "configs/selection/risk_v1.json")
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/shortline_close_event_overlay_v1_20261006"))
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    validate_config(config)
    events, sessions, manifests = load_complete_history(
        args.history_dir, config["primary_overlap_start"],
        config["primary_overlap_end"])
    expected_months = set(pd.period_range(
        str(config["primary_overlap_start"]),
        str(config["primary_overlap_end"]), freq="M").astype(str).str.replace("-", ""))
    complete_months = {str(item["month"]) for item in manifests}
    missing_months = sorted(expected_months - complete_months)
    if missing_months:
        raise RuntimeError("complete event months missing: {}".format(
            ",".join(missing_months)))
    predictions = pd.read_csv(
        args.predictions,
        usecols=["signal_asof", "symbol", "column", "ridge_score",
                 "excess_return_20d", "target_rank"],
        dtype={"symbol": str})
    predictions = predictions[
        predictions.signal_asof.between(
            config["primary_overlap_start"], config["primary_overlap_end"])]
    missing_event_dates = sorted(
        set(predictions.signal_asof.astype(int).unique()) - set(sessions))
    if missing_event_dates:
        raise RuntimeError("complete event sessions missing: {}".format(
            ",".join(str(value) for value in missing_event_dates[:20])))
    daily, stock = build_close_event_features(events, sessions)
    attached = attach_close_event_features(
        predictions, daily, stock, config["primary_input_score"])

    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    if policy.strategy_version != config["source_strategy_version"]:
        raise ValueError("source strategy version differs from registration")
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("source and low-turnover policy hashes differ")
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=int(config["primary_overlap_end"]))
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    args.output_dir.mkdir(parents=True)
    registration = {
        "registered_at": datetime.now().astimezone().isoformat(),
        "experiment": config,
        "config_sha256": digest(args.config),
        "predictions_path": str(args.predictions),
        "predictions_sha256": digest(args.predictions),
        "history_manifests": [{
            "month": item["month"], "batch_id": item["batch_id"],
            "parsed_sha256": item["parsed_sha256"],
            "availability_evidence": item["availability_evidence"],
            "strict_pit_allowed": item["strict_pit_allowed"],
        } for item in manifests],
        "excluded_fields": config["excluded_from_primary"],
        "source_config_sha256": source.sha256,
        "policy_config_sha256": policy.sha256,
        "risk_config_sha256": risk.sha256,
        "automatic_admission": False,
    }
    write_json(args.output_dir / "registration.json", registration)
    meta, folds = generate_meta_predictions(attached, panel.dates, config)
    write_json(args.output_dir / "fold_manifests.json", folds)
    meta.to_csv(args.output_dir / "meta_oos_predictions.csv.gz", index=False,
                compression={"method": "gzip", "compresslevel": 3})
    daily.to_csv(args.output_dir / "daily_event_factors.csv", index=False)
    ic, uplift, summary, top, primary = factor_report(meta, config)
    ic.to_csv(args.output_dir / "daily_rank_ic.csv", index=False)
    uplift.to_csv(args.output_dir / "daily_top10_uplift.csv", index=False)
    summary.to_csv(args.output_dir / "factor_summary.csv", index=False)
    top.to_csv(args.output_dir / "top10_summary.csv", index=False)

    portfolio, annual = run_portfolios(
        meta, panel, config, source, policy, risk, args.output_dir)
    portfolio.to_csv(args.output_dir / "portfolio_results.csv", index=False)
    annual.to_csv(args.output_dir / "annual_returns.csv", index=False)
    report = {
        "experiment_id": config["experiment_id"],
        "role": config["research_status"],
        "event_date_start": int(events.trade_date.min()),
        "event_date_end": int(events.trade_date.max()),
        "meta_oos_start": int(meta.signal_asof.min()),
        "meta_oos_end": int(meta.signal_asof.max()),
        "meta_oos_dates": int(meta.signal_asof.nunique()),
        "factor_primary_comparisons": primary,
        "portfolio_results": portfolio.to_dict("records"),
        "automatic_admission": False,
        "warning": "Historical eltdx records were queried in 2026 and are "
                   "not strict PIT evidence. Results only screen hypotheses "
                   "for the prospective shadow sample.",
    }
    write_json(args.output_dir / "report.json", report)
    write_json(args.output_dir / "completion.json", {
        "status": "complete", "completed_at": datetime.now().astimezone().isoformat(),
        "report_sha256": digest(args.output_dir / "report.json"),
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
