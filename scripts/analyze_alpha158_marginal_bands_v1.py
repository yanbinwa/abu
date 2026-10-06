#!/usr/bin/env python3
"""Diagnose cost-adjusted marginal rank bands for Alpha158 research arms."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_alpha158_lite_v1 import (  # noqa: E402
    moving_block_mean_interval,
)
from scripts.validate_alpha158_ranker_nonlinear_v1 import (  # noqa: E402
    canonical_hash, write_json,
)


DEFAULT_INPUTS = {
    "regression_trend": Path(
        "/Users/wjy/abu/backtests/alpha158_canonical_regression_trend_v1_20261006/oos_predictions.csv.gz"),
    "price_position": Path(
        "/Users/wjy/abu/backtests/alpha158_canonical_price_position_v1_20261006/oos_predictions.csv.gz"),
    "volume_structure": Path(
        "/Users/wjy/abu/backtests/alpha158_canonical_volume_structure_v1_20261006/oos_predictions.csv.gz"),
    "price_volume_persistence": Path(
        "/Users/wjy/abu/backtests/alpha158_canonical_price_volume_persistence_v1_20261006/oos_predictions.csv.gz"),
}


def daily_band_returns(frame, score_column, bands, cost):
    rows = []
    usable = frame.dropna(subset=[score_column, "excess_return_20d"]).copy()
    usable = usable.sort_values(
        ["signal_asof", score_column, "symbol"],
        ascending=[True, False, True], kind="mergesort")
    usable["rank"] = usable.groupby("signal_asof").cumcount() + 1
    for start, end in bands:
        selected = usable[(usable["rank"] >= start) &
                          (usable["rank"] <= end)]
        daily = selected.groupby("signal_asof", sort=True).agg(
            securities=("symbol", "size"),
            gross_excess=("excess_return_20d", "mean"),
            nonpositive_excess_rate=("excess_return_20d", lambda value:
                                     float((value <= 0).mean())),
        ).reset_index()
        daily["net_excess"] = daily.gross_excess - cost
        daily["band"] = "{}-{}".format(start, end)
        rows.append(daily)
    return pd.concat(rows, ignore_index=True)


def summarize(arm, daily, config):
    output, yearly_rows = [], []
    expansion = tuple(config["expansion_gate"]["band"])
    for band, group in daily.groupby("band", sort=False):
        ordered = group.sort_values("signal_asof")
        ci = moving_block_mean_interval(
            ordered.net_excess,
            block_length=int(config["block_length_sessions"]),
            replicates=int(config["bootstrap_replicates"]),
            seed=int(config["bootstrap_seed"]))
        year_values = ordered.assign(
            year=ordered.signal_asof // 10000).groupby("year").agg(
                dates=("signal_asof", "size"),
                gross_excess=("gross_excess", "mean"),
                net_excess=("net_excess", "mean"),
                nonpositive_excess_rate=("nonpositive_excess_rate", "mean"),
            ).reset_index()
        year_values.insert(0, "band", band)
        year_values.insert(0, "arm", arm)
        yearly_rows.extend(year_values.to_dict("records"))
        positive_years = int((year_values.net_excess > 0).sum())
        start, end = (int(item) for item in band.split("-"))
        is_expansion = (start, end) == expansion
        gate = config["expansion_gate"]
        passed = bool(
            is_expansion and
            ordered.net_excess.mean() > float(gate["net_excess_mean_min"]) and
            ci[0] >= float(gate["net_excess_block_ci_low_min"]) and
            positive_years >= int(
                gate["positive_year_net_excess_min_count"]))
        output.append({
            "arm": arm, "band": band,
            "dates": int(len(ordered)),
            "mean_securities": float(ordered.securities.mean()),
            "gross_excess_mean": float(ordered.gross_excess.mean()),
            "net_excess_mean": float(ordered.net_excess.mean()),
            "net_excess_block_ci": [float(ci[0]), float(ci[1])],
            "positive_year_net_excess_count": positive_years,
            "nonpositive_excess_rate": float(
                ordered.nonpositive_excess_rate.mean()),
            "expansion_gate": "PASS" if passed else (
                "FAIL" if is_expansion else "NOT_APPLICABLE"),
        })
    return output, yearly_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=
                        ROOT / "configs/selection/alpha158_marginal_bands_research_v1.json")
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_marginal_bands_v1_20261006"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=False)
    write_json(args.output_dir / "registration.json", {
        "config": config, "config_sha256": canonical_hash(config),
        "inputs": {name: str(path) for name, path in DEFAULT_INPUTS.items()},
        "registered_before_results": True,
    })
    missing = [str(path) for path in DEFAULT_INPUTS.values()
               if not path.exists()]
    if missing:
        raise FileNotFoundError("missing prediction inputs: {}".format(missing))

    cost = float(config["round_trip_cost_bps"]) / 10000.0
    bands = [tuple(item) for item in config["bands"]]
    summaries, yearly, daily_outputs = [], [], []
    first = True
    for family, path in DEFAULT_INPUTS.items():
        frame = pd.read_csv(path, usecols=[
            "signal_asof", "symbol", "excess_return_20d",
            "ridge_score", "candidate_score"])
        arms = {family: "candidate_score"}
        if first:
            arms = {"ridge": "ridge_score", **arms}
            first = False
        for arm, score_column in arms.items():
            daily = daily_band_returns(frame, score_column, bands, cost)
            daily.insert(0, "arm", arm)
            summary, year_rows = summarize(arm, daily, config)
            summaries.extend(summary)
            yearly.extend(year_rows)
            daily_outputs.append(daily)
    summary_frame = pd.DataFrame(summaries)
    yearly_frame = pd.DataFrame(yearly)
    daily_frame = pd.concat(daily_outputs, ignore_index=True)
    summary_frame.to_csv(args.output_dir / "band_summary.csv", index=False)
    yearly_frame.to_csv(args.output_dir / "band_yearly.csv", index=False)
    daily_frame.to_csv(
        args.output_dir / "band_daily.csv.gz", index=False,
        compression={"method": "gzip", "compresslevel": 3})
    expansion = summary_frame[summary_frame.band == "11-20"].copy()
    report = {
        "experiment_id": config["experiment_id"],
        "round_trip_cost_bps": config["round_trip_cost_bps"],
        "arms": list(config["score_arms"]),
        "summary": summaries,
        "expansion_gate_pass_arms": expansion.loc[
            expansion.expansion_gate == "PASS", "arm"].tolist(),
        "decision": "NO_TARGET_POSITION_INCREASE" if not (
            expansion.expansion_gate == "PASS").any()
            else "ELIGIBLE_FOR_SEPARATE_TARGET_15_BACKTEST",
        "observed_history": True,
        "new_holdout": False,
        "warning": (
            "This is a signal-capacity diagnostic, not a portfolio backtest. "
            "Passing only permits a separately registered target-15 replay."),
    }
    write_json(args.output_dir / "report.json", report)
    write_json(args.output_dir / "completion.json", {
        "status": "COMPLETE", "decision": report["decision"],
        "report_sha256": canonical_hash(report),
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
