#!/usr/bin/env python3
"""Evaluate fixed all-factor ensembles on the current Alpha158 portfolio."""
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
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview  # noqa: E402
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuResearchStatistics import (  # noqa: E402
    benjamini_hochberg,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.audit_alpha158_canonical_familywise_v2 import (  # noqa: E402
    centered_block_p_positive,
)
from scripts.backtest_alpha158_event_exit_only_2025_2026 import (  # noqa: E402
    save_audit,
)
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402
from scripts.research_alpha158_lite_v1 import (  # noqa: E402
    daily_rank_ic, daily_selection_uplift, moving_block_mean_interval,
)


DEFAULT_CONFIG = ROOT / "configs/selection/alpha158_all_factor_utility_v1.json"
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_all_factor_utility_v1_20261006")
KEYS = ("signal_asof", "symbol", "column")
ARMS = ("baseline", "all_mean_rank", "all_median_rank")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, payload):
    Path(path).write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True,
        default=str, allow_nan=False) + "\n", encoding="utf-8")


def validate_config(config):
    if config["research_status"] != \
            "OBSERVED_HISTORY_SCREEN_ONLY_NOT_ADMITTED":
        raise ValueError("all-factor experiment must remain research only")
    if config["source_strategy_version"] != \
            "alpha158_price_only_no_rank_exit_v1":
        raise ValueError("current event-exit-only strategy is required")
    if config["review_overlay"] != "suppress_rank_exits":
        raise ValueError("ranking exits must remain disabled")
    if config["parameter_search_allowed"] is not False:
        raise ValueError("parameter search is prohibited")
    if config["automatic_admission"] is not False:
        raise ValueError("automatic admission is prohibited")
    if config["inventory"] != {
            "total_registered_factors": 183,
            "primary_strict_historical_factors": 161,
            "secondary_backfilled_factors": 11,
            "forward_only_factors": 11}:
        raise ValueError("factor inventory differs from frozen experiment")
    if set(config["arms"]) != set(ARMS):
        raise ValueError("utility arms differ from frozen experiment")
    if config["family_weights"] != "equal_no_estimation":
        raise ValueError("family weights may not be fitted")
    required_boolean_gates = (
        "all_cost_return_delta_positive",
        "all_cost_max_drawdown_not_worse",
        "all_cost_expected_shortfall_not_worse",
        "all_cost_liquidation_return_not_worse",
        "primary_cost_calmar_delta_positive",
    )
    if any(config["gates"].get(name) is not True
           for name in required_boolean_gates):
        raise ValueError("all frozen portfolio gates must remain enabled")


def validate_folds(directory, predictions):
    manifests = json.loads((Path(directory) / "fold_manifests.json").read_text())
    if predictions.duplicated(list(KEYS[:2])).any():
        raise ValueError("duplicate OOS prediction")
    if set(predictions.fold.unique()) != {item["fold"] for item in manifests}:
        raise ValueError("prediction folds differ from manifests")
    for fold in manifests:
        required = ("train_label_end", "validation_start",
                    "validation_label_end", "test_start")
        if any(name not in fold for name in required):
            raise ValueError("strict label-boundary evidence is missing")
        if not (int(fold["train_label_end"]) < int(fold["validation_start"]) and
                int(fold["validation_label_end"]) < int(fold["test_start"])):
            raise ValueError("walk-forward labels overlap")
        rows = predictions[predictions.fold.eq(fold["fold"])]
        if not rows.signal_asof.between(
                int(fold["test_start"]), int(fold["test_end"])).all():
            raise ValueError("prediction lies outside OOS fold")


def load_ensemble(config):
    base = None
    score_columns = []
    source_hashes = {}
    for family in config["primary_families"]:
        directory = Path(config["primary_sources"][family])
        prediction_path = directory / "oos_predictions.csv.gz"
        completion = directory / "completion.json"
        if not prediction_path.exists() or not completion.exists():
            raise FileNotFoundError("incomplete family source: {}".format(directory))
        frame = pd.read_csv(prediction_path, dtype={"symbol": str})
        validate_folds(directory, frame)
        frame = frame.sort_values(list(KEYS), kind="mergesort").reset_index(drop=True)
        source_hashes[family] = {
            "predictions": digest(prediction_path),
            "folds": digest(directory / "fold_manifests.json"),
            "completion": digest(completion),
        }
        if base is None:
            base = frame[[*KEYS, "excess_return_20d", "target_rank",
                          "ridge_score", "fold", "train_end"]].copy()
        else:
            if not base[list(KEYS)].equals(frame[list(KEYS)]):
                raise ValueError("family prediction keys do not align")
            if not np.allclose(base.ridge_score, frame.ridge_score,
                               rtol=0, atol=1e-10, equal_nan=True):
                raise ValueError("family baselines differ")
            if not np.allclose(base.target_rank, frame.target_rank,
                               rtol=0, atol=1e-10, equal_nan=True):
                raise ValueError("family labels differ")
        column = "family_{}".format(family)
        base[column] = frame.candidate_score.to_numpy(dtype=float)
        score_columns.append(column)
    rank_columns = []
    grouped = base.groupby("signal_asof", sort=False)
    for column in score_columns:
        rank_column = column + "_rank"
        base[rank_column] = grouped[column].rank(
            method="average", pct=True) - .5
        rank_columns.append(rank_column)
    base["baseline"] = base.ridge_score
    base["all_mean_rank"] = base[rank_columns].mean(axis=1)
    base["all_median_rank"] = base[rank_columns].median(axis=1)
    if base[list(ARMS)].isna().any().any():
        raise ValueError("ensemble construction produced missing scores")
    return base, source_hashes, rank_columns


def factor_metrics(predictions, config):
    baseline_ic = daily_rank_ic(predictions, "baseline").rename(
        columns={"ic": "baseline_ic"})
    baseline_top = daily_selection_uplift(
        predictions, "baseline", 10)[["signal_asof", "uplift"]].rename(
            columns={"uplift": "baseline_uplift"})
    rows, daily_ic, daily_top, pvalues = [], [], [], []
    block = int(config["bootstrap_block_sessions"])
    replicates = int(config["bootstrap_replicates"])
    seed = int(config["bootstrap_seed"])
    for offset, arm in enumerate(ARMS[1:]):
        candidate_ic = daily_rank_ic(predictions, arm).rename(
            columns={"ic": "candidate_ic"})
        paired = baseline_ic.merge(
            candidate_ic, on="signal_asof", validate="one_to_one")
        paired["delta"] = paired.candidate_ic - paired.baseline_ic
        candidate_top = daily_selection_uplift(
            predictions, arm, 10)[["signal_asof", "uplift"]].rename(
                columns={"uplift": "candidate_uplift"})
        top = baseline_top.merge(
            candidate_top, on="signal_asof", validate="one_to_one")
        top["delta"] = top.candidate_uplift - top.baseline_uplift
        ic_ci = moving_block_mean_interval(
            paired.delta, block_length=block, replicates=replicates,
            seed=seed + offset)
        top_ci = moving_block_mean_interval(
            top.delta, block_length=block, replicates=replicates,
            seed=seed + 100 + offset)
        pvalue = centered_block_p_positive(
            paired.delta, block_length=block, replicates=replicates,
            seed=seed + 200 + offset)
        pvalues.append(pvalue)
        paired["arm"] = arm
        top["arm"] = arm
        paired["year"] = paired.signal_asof.astype(int) // 10000
        rows.append({
            "arm": arm,
            "baseline_rank_ic_mean": float(paired.baseline_ic.mean()),
            "candidate_rank_ic_mean": float(paired.candidate_ic.mean()),
            "rank_ic_delta_mean": float(paired.delta.mean()),
            "rank_ic_delta_ci95_low": float(ic_ci[0]),
            "rank_ic_delta_ci95_high": float(ic_ci[1]),
            "rank_ic_delta_p_one_sided": float(pvalue),
            "baseline_top10_uplift_mean": float(
                top.baseline_uplift.mean()),
            "candidate_top10_uplift_mean": float(
                top.candidate_uplift.mean()),
            "top10_uplift_delta_mean": float(top.delta.mean()),
            "top10_uplift_delta_ci95_low": float(top_ci[0]),
            "top10_uplift_delta_ci95_high": float(top_ci[1]),
            "positive_year_count": int((
                paired.groupby("year").delta.mean() > 0).sum()),
        })
        daily_ic.append(paired)
        daily_top.append(top)
    qvalues = benjamini_hochberg(pvalues)
    gates = config["gates"]
    for row, qvalue in zip(rows, qvalues):
        row["rank_ic_delta_fdr_q"] = float(qvalue)
        row["factor_gate"] = "PASS" if (
            row["rank_ic_delta_mean"] > 0 and
            row["rank_ic_delta_ci95_low"] >= float(
                gates["rank_ic_delta_ci95_low_min"]) and
            row["rank_ic_delta_fdr_q"] <= float(config["fdr_q_max"]) and
            row["top10_uplift_delta_mean"] > 0 and
            row["top10_uplift_delta_ci95_low"] >= float(
                gates["top10_uplift_delta_ci95_low_min"]) and
            row["positive_year_count"] >= int(
                gates["positive_year_count_min"])) else "FAIL"
    return (pd.DataFrame(rows), pd.concat(daily_ic, ignore_index=True),
            pd.concat(daily_top, ignore_index=True))


def curve_cagr(curve):
    start = pd.Timestamp(str(int(curve.date.iloc[0])))
    end = pd.Timestamp(str(int(curve.date.iloc[-1])))
    years = (end - start).days / 365.25
    return float(((curve.capital.iloc[-1] / curve.capital.iloc[0]) **
                  (1 / years) - 1) * 100)


def paired_cagr_interval(base_curve, candidate_curve, config, seed):
    if not np.array_equal(base_curve.date, candidate_curve.date):
        raise ValueError("paired portfolio calendars differ")
    start = pd.Timestamp(str(int(base_curve.date.iloc[0])))
    end = pd.Timestamp(str(int(base_curve.date.iloc[-1])))
    years = (end - start).days / 365.25
    base = np.diff(np.log(base_curve.capital.to_numpy(dtype=float)))
    candidate = np.diff(np.log(
        candidate_curve.capital.to_numpy(dtype=float)))
    block = min(int(config["bootstrap_block_sessions"]), len(base))
    replicates = int(config["bootstrap_replicates"])
    rng = np.random.default_rng(int(seed))
    blocks = int(math.ceil(len(base) / block))
    starts = rng.integers(0, len(base), size=(replicates, blocks, 1))
    indices = (starts + np.arange(block)) % len(base)
    indices = indices.reshape(replicates, -1)[:, :len(base)]
    base_cagr = np.exp(base[indices].sum(axis=1) / years) - 1
    candidate_cagr = np.exp(candidate[indices].sum(axis=1) / years) - 1
    values = (candidate_cagr - base_cagr) * 100
    return [float(np.quantile(values, .025)),
            float(np.quantile(values, .975))]


def run_portfolios(predictions, panel, source, policy, risk, config, output):
    rows, annual_rows, curves = [], [], {}
    end_date = int(predictions.signal_asof.max())
    for cost in config["slippage_bps"]:
        for arm in ARMS:
            key = "{}_{}bp".format(arm, int(cost))
            scores = rank_frame(
                predictions[[*KEYS, arm]].rename(columns={arm: "alpha_score"}),
                "alpha_score", max(
                    policy.entry_rank_limit, policy.retention_rank_limit))
            result, audit = run_low_turnover(
                panel, scores,
                replace(source, label_slippage_bps=float(cost)),
                policy, risk, end_date,
                review_overlay=CostAwareReview(suppress_rank_exits=True))
            save_audit(output / key, audit)
            curve = audit["curve"].copy()
            curves[key] = curve
            cagr = curve_cagr(curve)
            drawdown = abs(float(result["max_drawdown_pct"]))
            result.update({
                "key": key, "arm": arm, "slippage_bps": float(cost),
                "cagr_pct": cagr,
                "calmar": cagr / drawdown if drawdown else np.nan,
                "return_per_average_exposure": (
                    float(result["return_pct"]) /
                    float(result["average_exposure_pct"]) if
                    result["average_exposure_pct"] else np.nan),
                "construction": config["source_strategy_version"],
            })
            rows.append(result)
            yearly = annual_returns(curve)
            yearly.insert(0, "key", key)
            yearly.insert(1, "arm", arm)
            yearly.insert(2, "slippage_bps", float(cost))
            annual_rows.append(yearly)
            print(json.dumps({
                "key": key, "return_pct": result["return_pct"],
                "cagr_pct": cagr,
                "max_drawdown_pct": result["max_drawdown_pct"],
                "average_exposure_pct": result["average_exposure_pct"],
            }), flush=True)
    return (pd.DataFrame(rows), pd.concat(annual_rows, ignore_index=True),
            curves)


def portfolio_metrics(portfolio, curves, factor, config):
    comparisons = []
    gates = config["gates"]
    factor_by_arm = factor.set_index("arm")
    for arm_index, arm in enumerate(ARMS[1:]):
        costs = []
        for cost in config["slippage_bps"]:
            indexed = portfolio[portfolio.slippage_bps.eq(cost)].set_index("arm")
            baseline = indexed.loc["baseline"]
            candidate = indexed.loc[arm]
            comparison = {
                "arm": arm, "slippage_bps": float(cost),
                "return_delta_pp": float(
                    candidate.return_pct - baseline.return_pct),
                "cagr_delta_pp": float(
                    candidate.cagr_pct - baseline.cagr_pct),
                "max_drawdown_delta_pp": float(
                    candidate.max_drawdown_pct - baseline.max_drawdown_pct),
                "expected_shortfall_delta_pp": float(
                    candidate.daily_expected_shortfall_95_pct -
                    baseline.daily_expected_shortfall_95_pct),
                "liquidation_return_delta_pp": float(
                    candidate.liquidation_3_limits_return_pct -
                    baseline.liquidation_3_limits_return_pct),
                "average_exposure_delta_pp": float(
                    candidate.average_exposure_pct -
                    baseline.average_exposure_pct),
                "calmar_delta": float(candidate.calmar - baseline.calmar),
                "return_per_exposure_delta": float(
                    candidate.return_per_average_exposure -
                    baseline.return_per_average_exposure),
                "paired_cagr_delta_ci95": paired_cagr_interval(
                    curves["baseline_{}bp".format(int(cost))],
                    curves["{}_{}bp".format(arm, int(cost))],
                    config, int(config["bootstrap_seed"]) +
                    arm_index * 10 + int(cost)),
            }
            costs.append(comparison)
            comparisons.append(comparison)
        primary = next(row for row in costs
                       if math.isclose(row["slippage_bps"], 25.0))
        passed = (
            factor_by_arm.loc[arm, "factor_gate"] == "PASS" and
            primary["cagr_delta_pp"] >= float(
                gates["primary_cost_cagr_delta_pp_min"]) and
            primary["calmar_delta"] > 0 and
            primary["paired_cagr_delta_ci95"][0] >= 0 and
            all(row["return_delta_pp"] > 0 for row in costs) and
            all(row["max_drawdown_delta_pp"] >= 0 for row in costs) and
            all(row["expected_shortfall_delta_pp"] >= 0 for row in costs) and
            all(row["liquidation_return_delta_pp"] >= 0 for row in costs) and
            all(abs(row["average_exposure_delta_pp"]) <= float(
                gates["max_absolute_exposure_delta_pp"]) for row in costs))
        for row in comparisons:
            if row["arm"] == arm:
                row["portfolio_gate"] = "PASS" if passed else "FAIL"
    return pd.DataFrame(comparisons)


def secondary_evidence(config):
    rows = []
    for name, path in config["secondary_evidence"].items():
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        rows.append({
            "group": name, "path": path, "sha256": digest(path),
            "decision": (payload.get("decision") or
                         payload.get("promotion_decision") or
                         payload.get("research_gate")),
            "strict_pit": False,
            "eligible_for_primary_ensemble": False,
        })
    for name in config["forward_only_groups"]:
        rows.append({
            "group": name, "path": "", "sha256": "",
            "decision": "FORWARD_SHADOW_ONLY_NO_HISTORICAL_PIT_TEST",
            "strict_pit": False,
            "eligible_for_primary_ensemble": False,
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
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
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    validate_config(config)
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    args.output_dir.mkdir(parents=True)
    write_json(args.output_dir / "registration.json", {
        "registered_at": datetime.now().astimezone().isoformat(),
        "experiment": config, "config_sha256": digest(args.config),
        "registered_before_results": True,
        "candidate_count": 2, "parameter_search_performed": False,
        "automatic_admission": False,
    })
    predictions, source_hashes, rank_columns = load_ensemble(config)
    write_json(args.output_dir / "source_hashes.json", source_hashes)
    predictions[[*KEYS, "excess_return_20d", "target_rank", *ARMS]].to_csv(
        args.output_dir / "ensemble_oos_predictions.csv.gz", index=False,
        compression={"method": "gzip", "compresslevel": 3})
    factor, daily_ic, daily_top = factor_metrics(predictions, config)
    factor.to_csv(args.output_dir / "factor_metrics.csv", index=False)
    daily_ic.to_csv(args.output_dir / "paired_daily_ic.csv", index=False)
    daily_top.to_csv(args.output_dir / "paired_top10_uplift.csv", index=False)

    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    if policy.strategy_version != config["source_policy_version"] or \
            source.sha256 != policy.source_config_sha256:
        raise ValueError("source policy differs from registration")
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=int(predictions.signal_asof.max()))
    portfolio, annual, curves = run_portfolios(
        predictions, panel, source, policy, risk, config, args.output_dir)
    portfolio.to_csv(args.output_dir / "portfolio_results.csv", index=False)
    annual.to_csv(args.output_dir / "annual_returns.csv", index=False)
    comparisons = portfolio_metrics(portfolio, curves, factor, config)
    comparisons.to_csv(
        args.output_dir / "portfolio_comparisons.csv", index=False)
    secondary = secondary_evidence(config)
    write_json(args.output_dir / "secondary_evidence.json", secondary)
    passed = sorted(set(comparisons.loc[
        comparisons.portfolio_gate.eq("PASS"), "arm"]))
    decision = ("RETAIN_FOR_NEW_FORWARD_SHADOW_ONLY" if passed else
                "REJECT_ALL_FACTOR_ENSEMBLES")
    report = {
        "experiment_id": config["experiment_id"],
        "role": config["research_status"],
        "inventory": config["inventory"],
        "primary_families": config["primary_families"],
        "family_rank_columns": rank_columns,
        "oos_start": int(predictions.signal_asof.min()),
        "oos_end": int(predictions.signal_asof.max()),
        "oos_dates": int(predictions.signal_asof.nunique()),
        "factor_metrics": factor.to_dict("records"),
        "portfolio_results": portfolio.to_dict("records"),
        "portfolio_comparisons": comparisons.to_dict("records"),
        "secondary_evidence": secondary,
        "passed_arms": passed, "decision": decision,
        "parameter_search_performed": False,
        "automatic_admission": False,
        "observed_history": True, "new_holdout": False,
        "warning": (
            "All dates have been observed previously. A pass can only justify "
            "a newly frozen forward shadow, never immediate strategy adoption."),
    }
    write_json(args.output_dir / "report.json", report)
    write_json(args.output_dir / "completion.json", {
        "status": "COMPLETE", "decision": decision,
        "report_sha256": digest(args.output_dir / "report.json"),
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
