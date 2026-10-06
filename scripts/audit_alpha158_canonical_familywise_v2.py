#!/usr/bin/env python3
"""Family-wise audit for pre-registered Alpha158 canonical feature arms."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuResearchStatistics import (  # noqa: E402
    benjamini_hochberg,
)
from scripts.research_alpha158_lite_v1 import (  # noqa: E402
    moving_block_mean_interval,
)
from scripts.validate_alpha158_ranker_nonlinear_v1 import (  # noqa: E402
    canonical_hash, write_json,
)


def centered_block_p_positive(values, block_length=20, replicates=5000,
                              seed=20261006):
    """One-sided p-value for a positive serial mean under a centered null."""
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return np.nan
    observed = float(values.mean())
    centered = values - observed
    block_length = max(1, min(int(block_length), len(centered)))
    blocks = int(np.ceil(len(centered) / block_length))
    offsets = np.arange(block_length)
    rng = np.random.default_rng(seed)
    exceed = 0
    for _ in range(int(replicates)):
        starts = rng.integers(0, len(centered), size=blocks)
        indices = ((starts[:, None] + offsets) % len(centered)).ravel()
        null_mean = float(centered[indices[:len(centered)]].mean())
        exceed += int(null_mean >= observed)
    return float((exceed + 1) / (int(replicates) + 1))


def audit_family(family, directory, config, family_index):
    paired = pd.read_csv(directory / "paired_daily_ic.csv")
    uplift = pd.read_csv(directory / "paired_top10_uplift.csv")
    report = json.loads((directory / "report.json").read_text(
        encoding="utf-8"))
    test = config["familywise_testing"]
    seed = int(test["bootstrap_seed"]) + int(family_index)
    ic_values = paired.ic_delta.to_numpy(dtype=float)
    uplift_values = uplift.uplift_delta.to_numpy(dtype=float)
    ic_ci = moving_block_mean_interval(
        ic_values, block_length=int(test["block_length_sessions"]),
        replicates=int(test["bootstrap_replicates"]), seed=seed)
    uplift_ci = moving_block_mean_interval(
        uplift_values, block_length=int(test["block_length_sessions"]),
        replicates=int(test["bootstrap_replicates"]), seed=seed + 100)
    return {
        "family": family,
        "directory": str(directory),
        "oos_dates": int(len(paired)),
        "rank_ic_delta_mean": float(np.mean(ic_values)),
        "rank_ic_delta_block_ci": [float(ic_ci[0]), float(ic_ci[1])],
        "rank_ic_delta_p_one_sided": centered_block_p_positive(
            ic_values, block_length=int(test["block_length_sessions"]),
            replicates=int(test["bootstrap_replicates"]), seed=seed + 200),
        "top10_uplift_delta_mean": float(np.mean(uplift_values)),
        "top10_uplift_delta_block_ci": [
            float(uplift_ci[0]), float(uplift_ci[1])],
        "positive_year_ic_delta_count": int(
            report["factor"]["positive_year_ic_delta_count"]),
        "portfolio_return_delta_pct_points": float(
            report["portfolio_comparison"]["return_delta_pct_points"]),
        "max_drawdown_delta_pct_points": float(
            report["portfolio_comparison"][
                "max_drawdown_delta_pct_points"]),
        "liquidation_return_delta_pct_points": float(
            report["portfolio_comparison"][
                "liquidation_return_delta_pct_points"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=
                        ROOT / "configs/selection/alpha158_canonical_families_research_v2.json")
    parser.add_argument("--result-root", type=Path,
                        default=Path("/Users/wjy/abu/backtests"))
    parser.add_argument("--result-suffix", default="v2_20261006")
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_canonical_familywise_audit_v2_20261006"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=False)
    suffixes = config["familywise_testing"].get(
        "result_suffix_by_family", {})
    directories = {
        family: args.result_root / "alpha158_canonical_{}_{}".format(
            family, suffixes.get(family, args.result_suffix))
        for family in config["families"]
    }
    write_json(args.output_dir / "registration.json", {
        "config": config,
        "config_sha256": canonical_hash(config),
        "input_directories": {name: str(path)
                              for name, path in directories.items()},
        "registered_before_results": True,
    })
    missing = [str(path) for path in directories.values()
               if not (path / "completion.json").exists()]
    if missing:
        raise FileNotFoundError("incomplete family results: {}".format(missing))
    rows = [audit_family(family, directories[family], config, index)
            for index, family in enumerate(config["families"])]
    p_values = [row["rank_ic_delta_p_one_sided"] for row in rows]
    q_values = benjamini_hochberg(p_values)
    gates = config["gates"]
    q_max = float(config["familywise_testing"]["fdr_q_max"])
    for row, q_value in zip(rows, q_values):
        row["rank_ic_delta_fdr_q"] = float(q_value)
        row["primary_gate"] = "PASS" if (
            row["rank_ic_delta_mean"] > 0 and
            row["rank_ic_delta_block_ci"][0] >= float(
                gates["paired_ic_delta_block_ci_low_min"]) and
            q_value <= q_max) else "FAIL"
        row["secondary_gate"] = "PASS" if (
            row["primary_gate"] == "PASS" and
            row["top10_uplift_delta_mean"] > float(
                gates["top10_uplift_delta_min"]) and
            row["top10_uplift_delta_block_ci"][0] >= float(
                gates["top10_uplift_delta_block_ci_low_min"]) and
            row["positive_year_ic_delta_count"] >= int(
                gates["positive_year_ic_delta_min_count"])) else "FAIL"
        row["portfolio_gate"] = "PASS" if (
            row["secondary_gate"] == "PASS" and
            row["portfolio_return_delta_pct_points"] > float(
                gates["portfolio_return_delta_pct_points_min"]) and
            row["max_drawdown_delta_pct_points"] >= -float(
                gates["max_drawdown_worsening_limit_pct_points"]) and
            row["liquidation_return_delta_pct_points"] > float(
                gates["liquidation_return_delta_pct_points_min"])) else "FAIL"
        row["familywise_decision"] = (
            "ELIGIBLE_FOR_NEW_FORWARD_SHADOW_REGISTRATION"
            if row["portfolio_gate"] == "PASS"
            else "REJECT_OBSERVED_HISTORY_SCREEN")
    frame = pd.DataFrame(rows)
    frame.to_csv(args.output_dir / "familywise_metrics.csv", index=False)
    retained = frame.loc[
        frame.familywise_decision ==
        "ELIGIBLE_FOR_NEW_FORWARD_SHADOW_REGISTRATION", "family"].tolist()
    report = {
        "experiment_id": config["experiment_id"],
        "primary_metric": config["familywise_testing"]["primary_metric"],
        "multiple_testing": {
            "method": config["familywise_testing"]["correction"],
            "fdr_q_max": q_max,
            "family_count": len(rows),
        },
        "families": rows,
        "retained_families": retained,
        "decision": "REJECT_ALL_FAMILIES" if not retained
                    else "FORWARD_SHADOW_REGISTRATION_REQUIRED",
        "observed_history": True,
        "new_holdout": False,
        "warning": (
            "Passing observed-history gates cannot admit a trading strategy; "
            "it only permits a newly frozen forward shadow."),
    }
    write_json(args.output_dir / "report.json", report)
    write_json(args.output_dir / "completion.json", {
        "status": "COMPLETE", "decision": report["decision"],
        "report_sha256": canonical_hash(report),
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
