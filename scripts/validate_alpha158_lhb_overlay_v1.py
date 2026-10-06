#!/usr/bin/env python3
"""Validate one frozen lagged Dragon-Tiger List factor family."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
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
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview  # noqa: E402
from abupy.AlphaBu.ABuLongHuBangFactors import (  # noqa: E402
    DATASET_VERSION, LHB_FEATURES, build_lhb_features, load_complete_history,
)
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.validate_shortline_close_event_overlay_v1 import (  # noqa: E402
    daily_rank_ic, daily_top10_uplift, generate_meta_predictions,
    moving_block_means, run_portfolios, write_json,
)


DEFAULT_CONFIG = ROOT / "configs/selection/alpha158_lhb_overlay_v1.json"
DEFAULT_HISTORY = Path(
    "/Users/wjy/abu/data/selection_research/akshare_lhb_history_v1")
DEFAULT_PREDICTIONS = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_canonical_price_volume_persistence_v2_20261006/"
    "oos_predictions.csv.gz")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_lhb_overlay_event_exit_only_v1_20261006")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_config(config):
    if config["history_dataset_version"] != DATASET_VERSION:
        raise ValueError("LHB history dataset version differs")
    if config["research_status"] != "RETROSPECTIVE_SCREEN_ONLY_NOT_ADMITTED":
        raise ValueError("LHB history must remain retrospective research")
    if config["strict_pit"] is not False or \
            config["availability_evidence"] != "BACKFILLED_QUERY":
        raise ValueError("historical LHB data cannot be declared strict PIT")
    if config["automatic_admission"] is not False:
        raise ValueError("historical LHB research cannot auto-admit")
    if config["parameter_search_allowed"] is not False:
        raise ValueError("LHB factor search must remain disabled")
    if int(config["availability_lag_sessions"]) < 1:
        raise ValueError("LHB fields must lag at least one trading session")
    if int(config["label_horizon_sessions"]) != 20:
        raise ValueError("LHB experiment is frozen to 20-session labels")
    if config["model"] != "median_imputer_standard_scaler_ridge":
        raise ValueError("unregistered LHB meta model")
    if config["source_strategy_version"] != \
            "alpha158_price_only_no_rank_exit_v1":
        raise ValueError("LHB experiment must use the current strategy")
    if config["source_policy_version"] != \
            "alpha158_lite_low_turnover_v3":
        raise ValueError("LHB source policy differs")
    if config["review_overlay"] != "suppress_rank_exits":
        raise ValueError("LHB experiment must preserve event-exit-only policy")
    if set(config["factor_arms"]) != {"meta_base", "meta_lhb"}:
        raise ValueError("LHB experiment must have one fixed candidate")
    expected = ["base_rank_centered", *LHB_FEATURES]
    if config["factor_arms"]["meta_lhb"] != expected:
        raise ValueError("LHB features differ from frozen family")
    forbidden = set(config["excluded_from_primary"])
    if forbidden & set(config["factor_arms"]["meta_lhb"]):
        raise ValueError("future or narrative field entered LHB features")


def _block_interval(values, config, seed):
    samples = moving_block_means(
        values, config["bootstrap_block_sessions"],
        config["bootstrap_replicates"], seed)
    if not len(samples):
        return [np.nan, np.nan], np.nan
    return ([float(np.quantile(samples, .025)),
             float(np.quantile(samples, .975))],
            float((1 + (samples <= 0).sum()) / (len(samples) + 1)))


def factor_report(predictions, config):
    scores = ("ridge_score", "meta_base", "meta_lhb")
    ic = pd.concat([daily_rank_ic(predictions, score) for score in scores],
                   ignore_index=True)
    uplift = pd.concat([
        daily_top10_uplift(predictions, score) for score in scores],
        ignore_index=True)
    ic_pivot = ic.pivot(index="signal_asof", columns="score", values="rank_ic")
    top_pivot = uplift.pivot(
        index="signal_asof", columns="score", values="uplift")
    ic_delta = (ic_pivot.meta_lhb - ic_pivot.meta_base).dropna()
    top_delta = (top_pivot.meta_lhb - top_pivot.meta_base).dropna()
    ic_interval, ic_pvalue = _block_interval(ic_delta, config, 20261006)
    top_interval, top_pvalue = _block_interval(top_delta, config, 20261007)
    yearly = pd.DataFrame({"ic_delta": ic_delta}).reset_index()
    yearly["year"] = yearly.signal_asof.astype(int) // 10000
    yearly = yearly.groupby("year", as_index=False).agg(
        dates=("ic_delta", "size"), mean_ic_delta=("ic_delta", "mean"))
    positive_years = int((yearly.mean_ic_delta > 0).sum())
    gates = config["gates"]
    passed = (
        ic_interval[0] >= float(gates["rank_ic_delta_ci95_low_min"]) and
        top_interval[0] >= float(
            gates["top10_uplift_delta_ci95_low_min"]) and
        positive_years >= int(gates["positive_year_ic_delta_min_count"])
    )
    summary = {
        "meta_base_rank_ic_mean": float(ic_pivot.meta_base.mean()),
        "meta_lhb_rank_ic_mean": float(ic_pivot.meta_lhb.mean()),
        "rank_ic_delta_mean": float(ic_delta.mean()),
        "rank_ic_delta_ci95": ic_interval,
        "rank_ic_one_sided_block_pvalue": ic_pvalue,
        "meta_base_top10_uplift_mean": float(top_pivot.meta_base.mean()),
        "meta_lhb_top10_uplift_mean": float(top_pivot.meta_lhb.mean()),
        "top10_uplift_delta_mean": float(top_delta.mean()),
        "top10_uplift_delta_ci95": top_interval,
        "top10_one_sided_block_pvalue": top_pvalue,
        "positive_year_ic_delta_count": positive_years,
        "factor_gate": "PASS" if passed else "FAIL",
    }
    return ic, uplift, yearly, summary


def portfolio_gate(portfolio, config):
    comparisons = []
    for cost in sorted(portfolio.slippage_bps.unique()):
        rows = portfolio[portfolio.slippage_bps.eq(cost)].set_index("score")
        base = rows.loc["meta_base"]
        candidate = rows.loc["meta_lhb"]
        comparisons.append({
            "slippage_bps": float(cost),
            "return_delta_pct": float(
                candidate.return_pct - base.return_pct),
            "max_drawdown_delta_pct": float(
                candidate.max_drawdown_pct - base.max_drawdown_pct),
            "liquidation_return_delta_pct": float(
                candidate.liquidation_3_limits_return_pct -
                base.liquidation_3_limits_return_pct),
            "average_exposure_delta_pct": float(
                candidate.average_exposure_pct - base.average_exposure_pct),
            "filled_buy_delta": int(
                candidate.filled_buys - base.filled_buys),
        })
    primary = next(item for item in comparisons
                   if math.isclose(item["slippage_bps"], 25.0))
    gates = config["gates"]
    passed = (
        primary["return_delta_pct"] >= float(
            gates["primary_cost_return_delta_pct_min"]) and
        primary["max_drawdown_delta_pct"] >= -float(
            gates["primary_cost_max_drawdown_worsening_pct_max"]) and
        all(item["return_delta_pct"] >= 0 for item in comparisons)
    )
    return comparisons, "PASS" if passed else "FAIL"


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
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    validate_config(config)
    detail, institution, sessions, manifests = load_complete_history(
        args.history_dir, config["history_start"],
        config["primary_overlap_end"])
    expected_months = set(pd.period_range(
        str(config["history_start"]), str(config["primary_overlap_end"]),
        freq="M").astype(str).str.replace("-", ""))
    complete_months = {str(item["month"]) for item in manifests}
    missing_months = sorted(expected_months - complete_months)
    if missing_months:
        raise RuntimeError("complete LHB months missing: {}".format(
            ",".join(missing_months)))
    predictions = pd.read_csv(
        args.predictions,
        usecols=["signal_asof", "symbol", "column", "ridge_score",
                 "excess_return_20d", "target_rank"], dtype={"symbol": str})
    predictions = predictions[predictions.signal_asof.between(
        config["primary_overlap_start"], config["primary_overlap_end"])]

    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    if policy.strategy_version != config["source_policy_version"]:
        raise ValueError("source strategy version differs from registration")
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("source and low-turnover policy hashes differ")
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=int(config["primary_overlap_end"]))
    attached = build_lhb_features(
        predictions, detail, institution, sessions, panel.dates,
        rolling_sessions=config["rolling_sessions"],
        availability_lag_sessions=config["availability_lag_sessions"],
        score_column=config["primary_input_score"])
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    args.output_dir.mkdir(parents=True)
    registration = {
        "registered_at": datetime.now().astimezone().isoformat(),
        "experiment": config, "config_sha256": digest(args.config),
        "predictions_path": str(args.predictions),
        "predictions_sha256": digest(args.predictions),
        "history_manifests": [{
            "month": item["month"], "batch_id": item["batch_id"],
            "detail_sha256": item["detail_sha256"],
            "institution_sha256": item["institution_sha256"],
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
    feature_summary = attached[list(LHB_FEATURES)].describe().T.reset_index(
        names="feature")
    feature_summary.to_csv(args.output_dir / "feature_summary.csv", index=False)
    ic, uplift, yearly, factor = factor_report(meta, config)
    ic.to_csv(args.output_dir / "daily_rank_ic.csv", index=False)
    uplift.to_csv(args.output_dir / "daily_top10_uplift.csv", index=False)
    yearly.to_csv(args.output_dir / "yearly_ic_delta.csv", index=False)
    portfolio, annual = run_portfolios(
        meta, panel, config, source, policy, risk, args.output_dir,
        review_overlay=CostAwareReview(suppress_rank_exits=True))
    portfolio.to_csv(args.output_dir / "portfolio_results.csv", index=False)
    annual.to_csv(args.output_dir / "annual_returns.csv", index=False)
    comparisons, portfolio_status = portfolio_gate(portfolio, config)
    decision = ("RETAIN_FOR_FORWARD_SHADOW_ONLY"
                if factor["factor_gate"] == "PASS" and
                portfolio_status == "PASS" else
                "REJECT_FOR_PORTFOLIO_INTEGRATION")
    report = {
        "experiment_id": config["experiment_id"],
        "role": config["research_status"],
        "history_start": int(min(sessions)), "history_end": int(max(sessions)),
        "detail_rows": int(len(detail)),
        "institution_rows": int(len(institution)),
        "meta_oos_start": int(meta.signal_asof.min()),
        "meta_oos_end": int(meta.signal_asof.max()),
        "meta_oos_dates": int(meta.signal_asof.nunique()),
        "factor": factor, "yearly_ic_delta": yearly.to_dict("records"),
        "portfolio_results": portfolio.to_dict("records"),
        "portfolio_comparisons": comparisons,
        "portfolio_gate": portfolio_status, "decision": decision,
        "parameter_search_performed": False,
        "automatic_admission": False,
        "warning": "Historical Dragon-Tiger List records were queried in "
                   "2026 and are not strict PIT evidence. All factors use a "
                   "one-session availability lag and exclude provider forward "
                   "returns, interpretation and success-rate fields.",
    }
    write_json(args.output_dir / "report.json", report)
    write_json(args.output_dir / "completion.json", {
        "status": "complete",
        "completed_at": datetime.now().astimezone().isoformat(),
        "report_sha256": digest(args.output_dir / "report.json"),
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
