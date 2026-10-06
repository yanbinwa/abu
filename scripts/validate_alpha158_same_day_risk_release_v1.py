#!/usr/bin/env python3
"""Validate one frozen same-day-risk release candidate on strict OOS scores."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.validate_alpha158_annual_uplift_v1 import (
    curve_metrics, digest, paired_cagr_interval, validate_folds, write_json,
)


STRICT = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_canonical_price_volume_persistence_v2_20261006")


def liquidation_drawdown(curve):
    values = curve.liquidation_nav_3_limits.to_numpy(dtype=float)
    return float((1.0 - values / np.maximum.accumulate(values)).max() * 100.0)


def exposure_normalized_return(curve):
    """Arithmetic diagnostic per 252 full-exposure days; not levered NAV."""
    returns = curve.capital.pct_change().fillna(0.0).to_numpy(dtype=float)
    exposure = curve.exposure.shift(1).fillna(0.0).to_numpy(dtype=float)
    denominator = float(exposure.sum())
    if denominator <= 0:
        return float("nan")
    return float(returns.sum() / denominator * 252.0 * 100.0)


def freeze(args):
    from scripts.backtest_alpha158_event_exit_only_2025_2026 import data_paths

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    runtime = output / "runtime"
    for name in ("abupy", "scripts", "configs/selection"):
        shutil.copytree(
            ROOT / name, runtime / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    inputs = output / "inputs"
    inputs.mkdir()
    sources = {
        "strict.csv.gz": STRICT / "oos_predictions.csv.gz",
        "strict_folds.json": STRICT / "fold_manifests.json",
        "strict_report.json": STRICT / "report.json",
    }
    for name, source in sources.items():
        if not source.is_file():
            raise FileNotFoundError(source)
        shutil.copy2(source, inputs / name)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if config["candidate_count"] != 1 or config["parameter_search"]:
        raise ValueError("exactly one frozen candidate is required")
    market_hashes = {
        str(path): digest(path)
        for path in data_paths(args.signal_dir, args.research_dir)
    }
    write_json(output / "registration.json", {
        "registered_at": datetime.now().astimezone().isoformat(),
        "experiment": config,
        "input_sources": {name: str(path) for name, path in sources.items()},
        "input_hashes": {
            str(path): digest(path) for path in inputs.iterdir()},
        "market_hashes": market_hashes,
        "runtime_hashes": {
            str(path): digest(path)
            for path in runtime.rglob("*") if path.is_file()},
        "signal_dir": str(args.signal_dir),
        "research_dir": str(args.research_dir),
        "primary_prediction_column": "ridge_score",
        "excluded_prediction_columns": [
            "candidate_score", "target_rank", "excess_return_20d"],
        "parameter_search": False,
        "observed_history": True,
        "new_holdout": False,
        "automatic_admission": False,
    })
    os.execv(sys.executable, [
        sys.executable, "-B",
        str(runtime / "scripts" / Path(__file__).name),
        "--output", str(output), "--frozen",
    ])


def run(output):
    from abupy.AlphaBu.ABuAlpha158Lite import (
        load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
    )
    from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview
    from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config
    from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
    from scripts.backtest_alpha158_event_exit_only_2025_2026 import save_audit
    from scripts.backtest_alpha158_lite_low_turnover_v3 import (
        annual_returns, run_low_turnover,
    )
    from scripts.backtest_alpha158_lite_v1 import rank_frame

    registration = json.loads((output / "registration.json").read_text())
    hashes = {
        **registration["input_hashes"],
        **registration["runtime_hashes"],
        **registration["market_hashes"],
    }
    if any(digest(path) != expected for path, expected in hashes.items()):
        raise ValueError("registered inputs changed before replay")
    config = registration["experiment"]
    source = load_alpha158_lite_config(
        ROOT / "configs/selection/alpha158_lite_v1.json")
    policy = load_alpha158_lite_low_turnover_config(
        ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json")
    risk = load_risk_config(ROOT / "configs/selection/risk_v1.json")
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("policy/source mismatch")
    if not np.isclose(
            risk.same_day_new_risk_fraction,
            config["baseline_same_day_new_risk_fraction"]):
        raise ValueError("baseline same-day risk changed")
    if not np.isclose(
            risk.portfolio_open_risk_fraction,
            config["candidate_same_day_new_risk_fraction"]):
        raise ValueError("candidate must equal frozen total open-risk cap")
    candidate_risk = replace(
        risk, same_day_new_risk_fraction=float(
            config["candidate_same_day_new_risk_fraction"]))

    predictions = pd.read_csv(
        output / "inputs/strict.csv.gz",
        usecols=[
            "signal_asof", "symbol", "column", "ridge_score",
            "fold", "train_end"],
        dtype={"symbol": str})
    validate_folds(
        json.loads((output / "inputs/strict_folds.json").read_text()),
        predictions)
    panel = SelectionPanelV2.from_research_data(
        registration["signal_dir"], registration["research_dir"],
        start_date=20200101, end_date=20260930)
    mapped = predictions.symbol.map(panel.symbol_index)
    if mapped.isna().any() or not np.array_equal(
            mapped.to_numpy(), predictions.column.to_numpy()):
        raise ValueError("prediction security index differs from market panel")
    scores = rank_frame(
        predictions.rename(columns={"ridge_score": "alpha_score"}),
        "alpha_score",
        max(policy.entry_rank_limit, policy.retention_rank_limit))

    rows, curves, yearly = [], {}, []
    arms = (("baseline", risk), ("same_day_release", candidate_risk))
    for cost in config["slippage_bps"]:
        for arm, arm_risk in arms:
            key = "{}_{}bp".format(arm, int(cost))
            result, audit = run_low_turnover(
                panel, scores, replace(source, label_slippage_bps=float(cost)),
                policy, arm_risk, 20260930,
                review_overlay=CostAwareReview(suppress_rank_exits=True))
            curve = audit["curve"].copy()
            first = int(scores.signal_asof.min())
            if int(curve.date.iloc[0]) != first:
                anchor = {name: 0.0 for name in curve.columns}
                anchor.update(date=first, cash=1e6, capital=1e6)
                for name in curve.columns:
                    if name.startswith("liquidation_nav_"):
                        anchor[name] = 1e6
                curve = pd.concat(
                    [pd.DataFrame([anchor]), curve], ignore_index=True)
            curve["date"] = curve.date.astype(int)
            audit["curve"] = curve
            stats = curve_metrics(curve)
            stats["liquidation_drawdown_pct"] = liquidation_drawdown(curve)
            stats["exposure_normalized_return_pct"] = \
                exposure_normalized_return(curve)
            stats["filled_buys"] = int(result["filled_buys"])
            stats["risk_rejected"] = int(result["risk_rejected"])
            stats["risk_reduced"] = int(result["risk_reduced"])
            stats["entry_clusters"] = int(result["entry_clusters"])
            rows.append({
                "key": key, "arm": arm, "slippage_bps": float(cost),
                **stats})
            curves[key] = curve
            save_audit(output / key, audit)
            write_json(output / key / "executor_metrics.json", result)
            annual = annual_returns(curve)
            annual.insert(0, "key", key)
            yearly.append(annual)
            pd.DataFrame(rows).to_csv(output / "results.csv", index=False)
            print(json.dumps(rows[-1]), flush=True)

    yearly = pd.concat(yearly, ignore_index=True)
    yearly.to_csv(output / "annual_returns.csv", index=False)
    pairs = []
    for cost in config["slippage_bps"]:
        bkey = "baseline_{}bp".format(int(cost))
        ckey = "same_day_release_{}bp".format(int(cost))
        base = next(row for row in rows if row["key"] == bkey)
        candidate = next(row for row in rows if row["key"] == ckey)
        base_year = yearly[yearly.key.eq(bkey)].set_index("year").return_pct
        candidate_year = yearly[
            yearly.key.eq(ckey)].set_index("year").return_pct
        pair = {
            "slippage_bps": float(cost),
            "cagr_delta_pp": float(
                candidate["cagr_pct"] - base["cagr_pct"]),
            "drawdown_worsening_pp": float(
                candidate["max_drawdown_pct"] -
                base["max_drawdown_pct"]),
            "liquidation_drawdown_worsening_pp": float(
                candidate["liquidation_drawdown_pct"] -
                base["liquidation_drawdown_pct"]),
            "average_exposure_delta_pp": float(
                candidate["average_exposure_pct"] -
                base["average_exposure_pct"]),
            "exposure_normalized_return_delta_pp": float(
                candidate["exposure_normalized_return_pct"] -
                base["exposure_normalized_return_pct"]),
            "positive_years": int(
                (candidate_year - base_year > 0).sum()),
            **paired_cagr_interval(
                curves[bkey].capital, curves[ckey].capital,
                base["years"],
                block=int(config["bootstrap_block_sessions"]),
                replicates=int(config["bootstrap_replicates"]),
                seed=int(config["bootstrap_seed"])),
        }
        pairs.append(pair)
    pd.DataFrame(pairs).to_csv(output / "paired_results.csv", index=False)

    gates = config["gates"]
    primary = pairs[0]
    passed = bool(
        primary["cagr_delta_pp"] >=
        float(gates["primary_25bp_cagr_delta_pp_min"]) and
        primary["ci95_low_pp"] >
        float(gates["primary_ci95_low_pp_min"]) and
        primary["positive_years"] >= int(gates["positive_years_min"]) and
        all(pair["cagr_delta_pp"] >
            float(gates["all_cost_cagr_delta_pp_min"])
            for pair in pairs) and
        all(pair["drawdown_worsening_pp"] <=
            float(gates["max_drawdown_worsening_pp_max"])
            for pair in pairs) and
        all(pair["liquidation_drawdown_worsening_pp"] <=
            float(gates["liquidation_drawdown_worsening_pp_max"])
            for pair in pairs))
    if any(digest(path) != expected for path, expected in hashes.items()):
        raise ValueError("registered inputs changed during replay")
    decision = (
        "RETAIN_FOR_NEW_FORWARD_SHADOW_ONLY" if passed
        else "REJECT_HISTORICAL_SCREEN")
    report = {
        "experiment_id": config["experiment_id"],
        "decision": decision,
        "results": rows,
        "pairs": pairs,
        "strict_baseline_reproduced": True,
        "hashes_unchanged": True,
        "parameter_search": False,
        "observed_history": True,
        "hypothesis_informed_by_observed_rejection_outcomes": True,
        "new_holdout": False,
        "automatic_admission": False,
        "limitations": [
            "The hypothesis was formed after inspecting historical rejection outcomes.",
            "Exposure-normalized return is a non-executable arithmetic diagnostic.",
            "A historical pass permits only a separately registered forward shadow.",
        ],
    }
    write_json(output / "report.json", report)
    write_json(output / "completion.json", {
        "status": "COMPLETE", "decision": decision})
    print(json.dumps({"decision": decision, "pairs": pairs},
                     ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=Path(
            "/Users/wjy/abu/backtests/"
            "alpha158_same_day_risk_release_v1_20261006"))
    parser.add_argument(
        "--config", type=Path,
        default=ROOT / "configs/selection/"
        "alpha158_same_day_risk_release_research_v1.json")
    parser.add_argument(
        "--signal-dir", type=Path,
        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument(
        "--research-dir", type=Path,
        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--frozen", action="store_true")
    args = parser.parse_args()
    run(args.output.resolve()) if args.frozen else freeze(args)
