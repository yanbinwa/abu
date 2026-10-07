#!/usr/bin/env python3
"""Run the preregistered 2015--2026 all_mean_rank historical validation."""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime
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
from scripts.backtest_alpha158_event_exit_only_2025_2026 import save_audit  # noqa: E402
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402
from scripts.validate_alpha158_all_factor_utility_v1 import (  # noqa: E402
    paired_cagr_interval,
)
from scripts.validate_alpha158_canonical_family_v1 import (  # noqa: E402
    generate_predictions as generate_canonical,
)
from scripts.validate_alpha158_residual_overheat_v1 import (  # noqa: E402
    generate_predictions as generate_residual,
)


FAMILIES = (
    "regression_trend", "price_position", "volume_structure",
    "price_volume_persistence", "kbar_shape", "vwap_price",
    "residual_overheat",
)
COSTS = (25.0, 40.0, 60.0)
KEYS = ("signal_asof", "symbol", "column")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_history_2015_validation_v1_20261007")


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def write_json(path, payload):
    Path(path).write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False, default=str) + "\n", encoding="utf-8")


def frozen_research(template, validation):
    result = json.loads(json.dumps(template))
    walk = result["walk_forward"]
    walk["start_date"] = int(validation["walk_forward_signal_start_date"])
    walk["end_date"] = int(validation["evaluation_end_date"])
    result["experiment_id"] = validation["experiment_id"]
    result["research_status"] = validation["research_status"]
    result["parameter_search_performed"] = False
    result["observed_history"] = True
    result["new_holdout"] = True
    return result


def register(output, inputs, validation, canonical, residual):
    if output.exists():
        registration = json.loads((output / "registration.json").read_text())
        changed = [str(path) for path in inputs
                   if registration["input_hashes"].get(str(path)) != digest(path)]
        if changed:
            raise ValueError("registered input changed: " + ", ".join(changed))
        return registration
    output.mkdir(parents=True)
    frozen = output / "frozen_configs"
    frozen.mkdir()
    for path in inputs:
        shutil.copy2(path, frozen / path.name)
    registration = {
        "registered_at": datetime.now().astimezone().isoformat(),
        "experiment": validation,
        "canonical_research": canonical,
        "residual_research": residual,
        "families": list(FAMILIES),
        "costs_bps": list(COSTS),
        "input_hashes": {str(path): digest(path) for path in inputs},
        "parameter_search_performed": False,
        "family_or_feature_selection_performed": False,
        "automatic_admission": False,
        "financial_factors_in_primary_result": False,
        "historical_vendor_backfill": True,
    }
    write_json(output / "registration.json", registration)
    return registration


def family_predictions(output, panel, source, canonical, residual, family):
    directory = output / "families" / family
    prediction_path = directory / "oos_predictions.csv.gz"
    completion_path = directory / "completion.json"
    if completion_path.exists() and prediction_path.exists():
        completion = json.loads(completion_path.read_text())
        if digest(prediction_path) != completion["prediction_sha256"]:
            raise ValueError("completed family prediction changed: " + family)
        return pd.read_csv(prediction_path, dtype={"symbol": str})
    if directory.exists():
        raise RuntimeError("incomplete family directory requires review: " + str(directory))
    directory.mkdir(parents=True)
    print("GENERATE " + family, flush=True)
    if family == "residual_overheat":
        predictions, _, _ = generate_residual(
            panel, source, residual, directory)
    else:
        predictions, _, _ = generate_canonical(
            panel, source, canonical, family, directory)
    predictions.to_csv(
        prediction_path, index=False,
        compression={"method": "gzip", "compresslevel": 3})
    write_json(completion_path, {
        "status": "COMPLETE", "family": family,
        "rows": int(len(predictions)),
        "dates": int(predictions.signal_asof.nunique()),
        "prediction_sha256": digest(prediction_path),
    })
    return predictions


def build_ensemble(output, panel, source, canonical, residual, validation):
    base = None
    rank_columns = []
    for family in FAMILIES:
        frame = family_predictions(
            output, panel, source, canonical, residual, family)
        frame = frame.sort_values(list(KEYS), kind="mergesort").reset_index(drop=True)
        if base is None:
            base = frame[[*KEYS, "excess_return_20d", "target_rank",
                          "ridge_score", "fold", "train_end"]].copy()
        else:
            if not base[list(KEYS)].equals(frame[list(KEYS)]):
                raise ValueError("family prediction keys differ: " + family)
            if not np.allclose(base.ridge_score, frame.ridge_score,
                               rtol=0, atol=1e-10, equal_nan=True):
                raise ValueError("family Ridge baselines differ: " + family)
        rank = "family_" + family + "_rank"
        base[rank] = frame.groupby("signal_asof", sort=False).candidate_score.rank(
            method="average", pct=True).to_numpy(dtype=float) - .5
        rank_columns.append(rank)
        del frame
        gc.collect()
    base["baseline"] = base.ridge_score
    base["all_mean_rank"] = base[rank_columns].mean(axis=1)
    base = base[base.signal_asof.between(
        int(validation["evaluation_start_date"]),
        int(validation["evaluation_end_date"]))].reset_index(drop=True)
    if base[["baseline", "all_mean_rank"]].isna().any().any():
        raise ValueError("ensemble score contains missing values")
    path = output / "ensemble_oos_predictions.csv.gz"
    base[[*KEYS, "excess_return_20d", "target_rank", "baseline",
          "all_mean_rank", "fold", "train_end"]].to_csv(
              path, index=False,
              compression={"method": "gzip", "compresslevel": 3})
    return base, rank_columns


def max_drawdown(nav):
    nav = np.asarray(nav, dtype=float)
    drawdown = nav / np.maximum.accumulate(nav) - 1
    return float(np.nanmin(drawdown) * 100)


def segment_metrics(curve, start, end):
    work = curve[curve.date.between(int(start), int(end))].copy()
    if work.empty:
        raise ValueError("empty evaluation segment")
    capital = work.capital.to_numpy(dtype=float)
    liquidation = work.liquidation_nav_3_limits.to_numpy(dtype=float)
    daily = pd.Series(capital).pct_change().dropna()
    tail = daily[daily <= daily.quantile(.05)]
    start_date = pd.Timestamp(str(int(work.date.iloc[0])))
    end_date = pd.Timestamp(str(int(work.date.iloc[-1])))
    years = max((end_date - start_date).days / 365.25, 1 / 365.25)
    return {
        "start": int(work.date.iloc[0]), "end": int(work.date.iloc[-1]),
        "sessions": int(len(work)),
        "return_pct": float((capital[-1] / capital[0] - 1) * 100),
        "cagr_pct": float(((capital[-1] / capital[0]) ** (1 / years) - 1) * 100),
        "max_drawdown_pct": max_drawdown(capital),
        "daily_expected_shortfall_95_pct": float(tail.mean() * 100),
        "average_exposure_pct": float(work.exposure.mean() * 100),
        "liquidation_3_limits_return_pct": float(
            (liquidation[-1] / liquidation[0] - 1) * 100),
    }


def run_portfolios(output, panel, predictions, source, policy, risk, validation):
    rows, segments, annual_rows, curves = [], [], [], {}
    end_date = int(validation["evaluation_end_date"])
    for cost in COSTS:
        for arm in ("baseline", "all_mean_rank"):
            key = "{}_{}bp".format(arm, int(cost))
            scores = rank_frame(
                predictions[[*KEYS, arm]].rename(columns={arm: "alpha_score"}),
                "alpha_score", max(
                    policy.entry_rank_limit, policy.retention_rank_limit))
            result, audit = run_low_turnover(
                panel, scores, replace(source, label_slippage_bps=cost),
                policy, risk, end_date,
                review_overlay=CostAwareReview(suppress_rank_exits=True))
            save_audit(output / key, audit)
            curve = audit["curve"].copy()
            curves[(arm, cost)] = curve
            full = segment_metrics(
                curve, validation["evaluation_start_date"],
                validation["evaluation_end_date"])
            row = dict(result)
            row.update(full)
            row.update({"key": key, "arm": arm, "slippage_bps": cost})
            rows.append(row)
            for name, bounds in (
                    ("primary_holdout", validation["primary_new_holdout"]),
                    ("observed_supplement",
                     validation["previously_observed_supplement"])):
                segment = segment_metrics(
                    curve, bounds["start_date"], bounds["end_date"])
                segment.update({"segment": name, "arm": arm,
                                "slippage_bps": cost})
                segments.append(segment)
            yearly = annual_returns(curve)
            yearly = yearly[yearly.year.between(2015, 2026)]
            yearly["arm"] = arm
            yearly["slippage_bps"] = cost
            annual_rows.append(yearly)
            print(json.dumps({
                "key": key, "return_pct": full["return_pct"],
                "cagr_pct": full["cagr_pct"],
                "max_drawdown_pct": full["max_drawdown_pct"],
                "es95": full["daily_expected_shortfall_95_pct"],
                "average_exposure_pct": full["average_exposure_pct"],
            }), flush=True)
    return (pd.DataFrame(rows), pd.DataFrame(segments),
            pd.concat(annual_rows, ignore_index=True), curves)


def comparisons(portfolio, segments, annual, curves, validation):
    rows = []
    bootstrap = {
        "bootstrap_block_sessions": int(validation["bootstrap_block_sessions"]),
        "bootstrap_replicates": int(validation["bootstrap_replicates"]),
        "bootstrap_seed": int(validation["bootstrap_seed"]),
    }
    for cost in COSTS:
        full = portfolio[portfolio.slippage_bps.eq(cost)].set_index("arm")
        primary = segments[
            segments.slippage_bps.eq(cost) &
            segments.segment.eq("primary_holdout")].set_index("arm")
        base_curve = curves[("baseline", cost)]
        candidate_curve = curves[("all_mean_rank", cost)]
        start = int(validation["primary_new_holdout"]["start_date"])
        end = int(validation["primary_new_holdout"]["end_date"])
        base_part = base_curve[base_curve.date.between(start, end)]
        candidate_part = candidate_curve[candidate_curve.date.between(start, end)]
        interval = paired_cagr_interval(
            base_part.reset_index(drop=True),
            candidate_part.reset_index(drop=True), bootstrap,
            int(validation["bootstrap_seed"]) + int(cost))
        rows.append({
            "slippage_bps": cost,
            "full_return_delta_pp": float(
                full.loc["all_mean_rank", "return_pct"] -
                full.loc["baseline", "return_pct"]),
            "full_cagr_delta_pp": float(
                full.loc["all_mean_rank", "cagr_pct"] -
                full.loc["baseline", "cagr_pct"]),
            "full_max_drawdown_improvement_pp": float(
                full.loc["all_mean_rank", "max_drawdown_pct"] -
                full.loc["baseline", "max_drawdown_pct"]),
            "full_es95_improvement_pp": float(
                full.loc["all_mean_rank", "daily_expected_shortfall_95_pct"] -
                full.loc["baseline", "daily_expected_shortfall_95_pct"]),
            "full_average_exposure_delta_pp": float(
                full.loc["all_mean_rank", "average_exposure_pct"] -
                full.loc["baseline", "average_exposure_pct"]),
            "primary_return_delta_pp": float(
                primary.loc["all_mean_rank", "return_pct"] -
                primary.loc["baseline", "return_pct"]),
            "primary_cagr_delta_pp": float(
                primary.loc["all_mean_rank", "cagr_pct"] -
                primary.loc["baseline", "cagr_pct"]),
            "primary_max_drawdown_improvement_pp": float(
                primary.loc["all_mean_rank", "max_drawdown_pct"] -
                primary.loc["baseline", "max_drawdown_pct"]),
            "primary_es95_improvement_pp": float(
                primary.loc["all_mean_rank", "daily_expected_shortfall_95_pct"] -
                primary.loc["baseline", "daily_expected_shortfall_95_pct"]),
            "primary_liquidation_return_delta_pp": float(
                primary.loc["all_mean_rank", "liquidation_3_limits_return_pct"] -
                primary.loc["baseline", "liquidation_3_limits_return_pct"]),
            "primary_cagr_delta_ci95_low_pp": interval[0],
            "primary_cagr_delta_ci95_high_pp": interval[1],
        })
    result = pd.DataFrame(rows)
    annual25 = annual[annual.slippage_bps.eq(25.0)].pivot(
        index="year", columns="arm", values="return_pct")
    positive_years = int((
        annual25.all_mean_rank > annual25.baseline).sum())
    gates = validation["gates"]
    primary = result[result.slippage_bps.eq(25.0)].iloc[0]
    checks = {
        "primary_holdout_cagr_delta_ci95_low": bool(
            primary.primary_cagr_delta_ci95_low_pp >=
            float(gates["primary_holdout_cagr_delta_ci95_low_min"])),
        "primary_holdout_return_positive": bool(
            primary.primary_return_delta_pp > 0),
        "primary_holdout_drawdown_not_worse": bool(
            primary.primary_max_drawdown_improvement_pp >= 0),
        "primary_holdout_es95_not_worse": bool(
            primary.primary_es95_improvement_pp >= 0),
        "primary_holdout_liquidation_not_worse": bool(
            primary.primary_liquidation_return_delta_pp >= 0),
        "all_cost_return_delta_positive": bool(
            result.full_return_delta_pp.gt(0).all()),
        "full_period_drawdown_not_worse": bool(
            result.full_max_drawdown_improvement_pp.ge(0).all()),
        "positive_calendar_years": positive_years >= int(
            gates["positive_calendar_year_delta_min_count"]),
    }
    gate = {"checks": checks, "positive_calendar_years": positive_years,
            "passed": bool(all(checks.values())),
            "automatic_admission": False}
    return result, gate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_history_2015_validation_v1.json")
    parser.add_argument("--canonical-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_canonical_families_research_v4.json")
    parser.add_argument("--residual-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_residual_overheat_research_v1.json")
    parser.add_argument("--source-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--policy-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json")
    parser.add_argument("--risk-config", type=Path, default=
                        ROOT / "configs/selection/risk_v1.json")
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research_2015_v1"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    inputs = [args.validation_config, args.canonical_config,
              args.residual_config, args.source_config, args.policy_config,
              args.risk_config, Path(__file__)]
    validation = json.loads(args.validation_config.read_text())
    canonical = frozen_research(
        json.loads(args.canonical_config.read_text()), validation)
    residual = frozen_research(
        json.loads(args.residual_config.read_text()), validation)
    register(args.output_dir, inputs, validation, canonical, residual)
    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("source/policy configuration mismatch")
    print("LOAD PANEL", flush=True)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=int(validation["panel_start_date"]),
        end_date=int(validation["evaluation_end_date"]))
    predictions, rank_columns = build_ensemble(
        args.output_dir, panel, source, canonical, residual, validation)
    portfolio, segments, annual, curves = run_portfolios(
        args.output_dir, panel, predictions, source, policy, risk, validation)
    comparison, gate = comparisons(
        portfolio, segments, annual, curves, validation)
    portfolio.to_csv(args.output_dir / "portfolio_results.csv", index=False)
    segments.to_csv(args.output_dir / "segment_results.csv", index=False)
    annual.to_csv(args.output_dir / "annual_returns.csv", index=False)
    comparison.to_csv(args.output_dir / "comparisons.csv", index=False)
    write_json(args.output_dir / "gates.json", gate)
    report = {
        "status": "COMPLETE", "experiment_id": validation["experiment_id"],
        "evaluation_start": validation["evaluation_start_date"],
        "evaluation_end": validation["evaluation_end_date"],
        "families": list(FAMILIES), "family_rank_columns": rank_columns,
        "oos_dates": int(predictions.signal_asof.nunique()),
        "oos_rows": int(len(predictions)),
        "portfolio_results": portfolio.to_dict("records"),
        "segment_results": segments.to_dict("records"),
        "comparisons": comparison.to_dict("records"),
        "gate": gate,
        "decision": ("RETAIN_FOR_FORWARD_SHADOW_REVIEW" if gate["passed"]
                     else "REJECT_ON_PREREGISTERED_HISTORY_GATE"),
        "automatic_admission": False,
        "financial_factor_result": {
            "included_in_primary": False,
            "status": "SEPARATE_NON_STRICT_PIT_SENSITIVITY_ONLY",
            "coverage_audit": (
                "/Users/wjy/abu/data/selection_research/"
                "fundamental_full_market_2012_2016_quality_v2/"
                "exchange_year_delisted_coverage.json"),
            "reason": "historical vendor revision chain incomplete",
        },
    }
    write_json(args.output_dir / "report.json", report)
    write_json(args.output_dir / "completion.json", {
        "status": "COMPLETE", "decision": report["decision"],
        "report_sha256": digest(args.output_dir / "report.json")})
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
