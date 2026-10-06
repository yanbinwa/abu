#!/usr/bin/env python3
"""Evaluate frozen forward rank bands only after their 20-session labels mature."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu import ABuAlphaForwardShadow as shadow  # noqa: E402
from abupy.AlphaBu.ABuAlpha158Lite import Alpha158LiteFeatureEngine  # noqa: E402
from scripts import run_alpha158_forward_shadow_v1 as forward  # noqa: E402
from scripts.research_alpha158_lite_v1 import moving_block_mean_interval  # noqa: E402


DEFAULT_CONFIG = ROOT / "configs/selection/alpha158_forward_marginal_bands_v1.json"
DEFAULT_ROOT = Path("/Users/wjy/abu/shadow/alpha158_marginal_bands_forward_v1")
CODE_FILES = (
    "scripts/audit_alpha158_forward_marginal_bands_v1.py",
    "scripts/research_alpha158_lite_v1.py",
    "scripts/run_alpha158_forward_shadow_v1.py",
    "abupy/AlphaBu/ABuAlpha158Lite.py",
    "abupy/AlphaBu/ABuAlphaForwardShadow.py",
)


def canonical(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def code_integrity():
    return {name: shadow.sha256_file(ROOT / name) for name in CODE_FILES}


def initialize(config_path, root):
    if root.exists():
        raise FileExistsError("forward marginal-band registry already exists")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    source = Path(config["source_shadow_root"])
    shadow.verify_chain(source)
    root.mkdir(parents=True)
    shutil.copy2(config_path, root / "protocol.json")
    history = Path("/Users/wjy/abu/backtests/alpha158_marginal_bands_v1_20261006/report.json")
    registration = {
        "registered_at": shadow.now_shanghai().isoformat(),
        "protocol_sha256": canonical(config),
        "source_shadow_root": str(source),
        "source_shadow_registration_sha256": shadow.sha256_file(
            source / "genesis/registration.json"),
        "historical_diagnostic": str(history),
        "historical_diagnostic_sha256": shadow.sha256_file(history),
        "historical_decision": "NO_TARGET_POSITION_INCREASE",
        "prediction_source": "daily scores archived before future labels exist",
        "label_policy": "frozen Alpha158 excess-return label after exactly 20 appended sessions",
        "no_parameter_search": True,
        "automatic_target_position_increase": False,
        "paper_or_live_admission": False,
    }
    shadow.json_write(root / "registration.json", registration)
    shadow.json_write(root / "code_integrity.json", code_integrity())
    status = status_report(config, root)
    shadow.json_write(root / "status.json", status)
    return status


def verify(config, root):
    registration = json.loads((root / "registration.json").read_text(encoding="utf-8"))
    if canonical(config) != registration["protocol_sha256"]:
        raise ValueError("forward marginal-band protocol changed")
    if shadow.sha256_file(Path(config["source_shadow_root"]) /
                          "genesis/registration.json") != registration[
                              "source_shadow_registration_sha256"]:
        raise ValueError("source forward registration changed")
    frozen_code = json.loads((root / "code_integrity.json").read_text(
        encoding="utf-8"))
    if frozen_code != code_integrity():
        raise ValueError("forward marginal-band evaluator code changed")


def status_report(config, root):
    source = Path(config["source_shadow_root"])
    paths, _, state = forward.latest(source)
    matured = max(0, len(paths) - 1 - int(config["label_horizon_sessions"]))
    due = (matured >= int(config["minimum_matured_signal_dates"]) and
           state["evaluation_status"] == "REVIEW_REQUIRED")
    return {
        "status": ("READY_TO_EVALUATE" if due else
                   "WAITING_FOR_FORWARD_CHECKPOINT" if matured >= int(
                       config["minimum_matured_signal_dates"])
                   else "WAITING_FOR_FORWARD_LABELS"),
        "source_sessions": len(paths) - 1,
        "matured_signal_dates": matured,
        "minimum_matured_signal_dates": config["minimum_matured_signal_dates"],
        "source_evaluation_status": state["evaluation_status"],
        "last_processed_date": state["last_processed_date"],
        "target_positions_unchanged": True,
    }


def _daily_labels(config):
    source = Path(config["source_shadow_root"])
    paths, _, _ = forward.latest(source)
    panel = list(shadow.replay_panels(paths))[-1]
    engine = Alpha158LiteFeatureEngine(panel)
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    horizon = int(config["label_horizon_sessions"])
    rows = []
    for directory in paths[1:]:
        scores = pd.read_csv(directory / "features_scores.csv.gz",
                             dtype={"symbol": str})
        signal_date = int(scores.signal_asof.iloc[0])
        day = date_index[signal_date]
        if day + horizon >= len(panel.dates):
            continue
        labels = engine.snapshot(day, include_labels=True)[
            ["symbol", f"excess_return_{horizon}d"]]
        selected = scores[["signal_asof", "symbol", "daily_rank"]].merge(
            labels, on="symbol", how="left", validate="one_to_one")
        selected = selected.rename(columns={f"excess_return_{horizon}d": "gross_excess"})
        selected["label_end"] = int(panel.dates[day + horizon])
        rows.append(selected)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["signal_asof", "symbol", "daily_rank", "gross_excess", "label_end"])


def evaluate(config, root):
    verify(config, root)
    state = status_report(config, root)
    shadow.json_write(root / "status.json", state)
    if state["status"] != "READY_TO_EVALUATE":
        return state
    labels = _daily_labels(config)
    summaries, daily_rows = [], []
    expansion = "{}-{}".format(*config["expansion_band"])
    primary = float(config["primary_round_trip_cost_bps"])
    for start, end in (tuple(item) for item in config["bands"]):
        band = labels[labels.daily_rank.between(start, end)].copy()
        daily = band.groupby("signal_asof", sort=True).agg(
            label_end=("label_end", "first"),
            securities=("gross_excess", "count"),
            gross_excess=("gross_excess", "mean"),
        ).reset_index()
        name = f"{start}-{end}"
        daily["band"] = name
        daily["net_excess_primary"] = daily.gross_excess - primary / 10000
        daily_rows.append(daily)
        ordered = daily.dropna(subset=["net_excess_primary"]).sort_values("signal_asof")
        interval = moving_block_mean_interval(
            ordered.net_excess_primary,
            block_length=int(config["bootstrap_block_sessions"]),
            replicates=int(config["bootstrap_replicates"]),
            seed=int(config["bootstrap_seed"]))
        cohort_size = int(config["nonoverlapping_cohort_sessions"])
        cohorts = np.array_split(
            ordered.net_excess_primary.to_numpy(),
            np.arange(cohort_size, len(ordered), cohort_size))
        cohort_means = [float(np.mean(item)) for item in cohorts if len(item) == cohort_size]
        stress = {str(cost): float((ordered.gross_excess - cost / 10000).mean())
                  for cost in config["round_trip_cost_scenarios_bps"]}
        summaries.append({
            "band": name, "matured_dates": int(len(ordered)),
            "mean_securities": float(ordered.securities.mean()),
            "gross_excess_mean": float(ordered.gross_excess.mean()),
            "net_excess_primary_mean": float(ordered.net_excess_primary.mean()),
            "net_excess_primary_block_ci": [float(interval[0]), float(interval[1])],
            "nonoverlapping_cohort_count": len(cohort_means),
            "positive_cohort_fraction": (float(np.mean(np.asarray(cohort_means) > 0))
                                          if cohort_means else None),
            "cost_scenario_net_excess_mean": stress,
        })
    candidate = next(item for item in summaries if item["band"] == expansion)
    gates = {
        "enough_dates": candidate["matured_dates"] >= int(
            config["minimum_matured_signal_dates"]),
        "complete_band": candidate["mean_securities"] >= float(
            config["minimum_mean_securities_per_band"]),
        "positive_primary_mean": candidate["net_excess_primary_mean"] > 0,
        "positive_primary_block_ci_low": candidate[
            "net_excess_primary_block_ci"][0] > 0,
        "positive_all_cost_scenarios": all(
            value > 0 for value in candidate["cost_scenario_net_excess_mean"].values()),
        "positive_nonoverlapping_cohorts": (
            candidate["positive_cohort_fraction"] is not None and
            candidate["positive_cohort_fraction"] >= float(
                config["minimum_positive_cohort_fraction"])),
    }
    passed = all(gates.values())
    report = {
        "status": ("ELIGIBLE_FOR_SEPARATE_TARGET_15_REPLAY" if passed else
                   "KEEP_TARGET_10"),
        "asof": int(labels.label_end.max()), "summaries": summaries,
        "expansion_band": expansion, "gates": gates,
        "automatic_target_position_increase": False,
        "paper_or_live_admission": False,
        "warning": (
            "Passing permits one separately registered target-15 portfolio replay. "
            "It does not change the forward accounts or authorize paper/live expansion."),
    }
    destination = root / "evaluations" / str(report["asof"])
    if destination.exists():
        existing = json.loads((destination / "report.json").read_text(encoding="utf-8"))
        if canonical(existing) != canonical(report):
            raise ValueError("existing forward evaluation differs")
        return existing
    destination.mkdir(parents=True)
    pd.concat(daily_rows, ignore_index=True).to_csv(
        destination / "band_daily.csv.gz", index=False,
        compression={"method": "gzip", "compresslevel": 3})
    labels.to_csv(destination / "matured_labels.csv.gz", index=False,
                  compression={"method": "gzip", "compresslevel": 3})
    shadow.json_write(destination / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "status", "evaluate"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.command == "init":
        result = initialize(args.config, args.root)
    else:
        verify(config, args.root)
        result = status_report(config, args.root) if args.command == "status" else evaluate(config, args.root)
        shadow.json_write(args.root / "status.json", status_report(config, args.root))
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
