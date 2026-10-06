#!/usr/bin/env python3
"""Audit amount/volume VWAP units, coverage and adjustment mapping.

This script is a data-quality gate only.  It neither computes an alpha score
nor modifies a strategy configuration.
"""
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

from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.validate_alpha158_ranker_nonlinear_v1 import (  # noqa: E402
    canonical_hash, write_json,
)


def _fraction(numerator, denominator):
    return float(numerator / denominator) if denominator else np.nan


def range_membership(values, low, high, tolerance_bps):
    tolerance = float(tolerance_bps) / 10000.0
    valid = (np.isfinite(values) & np.isfinite(low) & np.isfinite(high) &
             (low > 0) & (high >= low))
    inside = valid & (values >= low * (1.0 - tolerance)) & \
        (values <= high * (1.0 + tolerance))
    return valid, inside


def evaluate_unit_candidates(amount, volume, close, low, high, candidates,
                             tolerance_bps, inside_min, ratio_min, ratio_max,
                             eligible=None):
    amount = np.asarray(amount, dtype=float)
    volume = np.asarray(volume, dtype=float)
    close = np.asarray(close, dtype=float)
    base_valid = (np.isfinite(amount) & (amount > 0) &
                  np.isfinite(volume) & (volume > 0) &
                  np.isfinite(close) & (close > 0))
    if eligible is not None:
        base_valid &= np.asarray(eligible, dtype=bool)
    base = np.divide(
        amount, volume, out=np.full(amount.shape, np.nan, dtype=float),
        where=base_valid)
    rows = []
    for multiplier in candidates:
        values = base * float(multiplier)
        comparable, inside = range_membership(
            values, low, high, tolerance_bps)
        valid = comparable & base_valid
        ratio = np.divide(
            values, close, out=np.full(values.shape, np.nan, dtype=float),
            where=base_valid)
        inside_fraction = _fraction(np.count_nonzero(inside & base_valid),
                                    np.count_nonzero(valid))
        median_ratio = float(np.nanmedian(ratio[base_valid])) \
            if np.any(base_valid) else np.nan
        recognized = bool(
            np.isfinite(inside_fraction) and inside_fraction >= inside_min and
            np.isfinite(median_ratio) and ratio_min <= median_ratio <= ratio_max)
        rows.append({
            "multiplier": float(multiplier),
            "comparable_rows": int(np.count_nonzero(valid)),
            "inside_raw_range_fraction": inside_fraction,
            "median_vwap_to_raw_close": median_ratio,
            "recognized": recognized,
        })
    return base, rows


def coverage_rows(panel, eligible, amount_valid):
    rows = []
    years = panel.dates // 10000
    exchanges = np.asarray([
        "sh" if symbol.startswith("sh") else "sz"
        for symbol in panel.symbols], dtype=object)
    for dimension, labels, axis in (
            ("year", sorted(set(years.tolist())), 0),
            ("exchange", ["sh", "sz"], 1)):
        for label in labels:
            selector = (years == label)[:, None] if axis == 0 else \
                (exchanges == label)[None, :]
            denominator = int(np.count_nonzero(eligible & selector))
            numerator = int(np.count_nonzero(amount_valid & selector))
            rows.append({
                "dimension": dimension, "value": str(label),
                "eligible_rows": denominator,
                "amount_rows": numerator,
                "coverage": _fraction(numerator, denominator),
            })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=
                        ROOT / "configs/selection/alpha158_vwap_scale_audit_v1.json")
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_vwap_scale_audit_v1_20261006"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=False)
    write_json(args.output_dir / "registration.json", {
        "config": config, "config_sha256": canonical_hash(config),
        "signal_dir": str(args.signal_dir),
        "research_dir": str(args.research_dir),
        "registered_before_full_audit": True,
        "warning": "Thresholds are data-quality gates, not alpha gates.",
    })

    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=int(config["start_date"]),
        end_date=int(config["end_date"]))
    raw_eligible = (
        panel.universe_mask & np.isfinite(panel.exec_open) &
        np.isfinite(panel.exec_high) & np.isfinite(panel.exec_low) &
        np.isfinite(panel.exec_close) & (panel.exec_close > 0) &
        np.isfinite(panel.exec_volume) & (panel.exec_volume > 0))
    amount_valid = raw_eligible & np.isfinite(panel.amount) & (panel.amount > 0)
    raw_vwap, candidates = evaluate_unit_candidates(
        panel.amount, panel.exec_volume, panel.exec_close,
        panel.exec_low, panel.exec_high,
        config["unit_multiplier_candidates"],
        config["raw_range_tolerance_bps"],
        config["candidate_inside_range_fraction_min"],
        config["candidate_median_close_ratio_min"],
        config["candidate_median_close_ratio_max"], eligible=raw_eligible)
    recognized = [row for row in candidates if row["recognized"]]
    unit_pass = len(recognized) == 1
    chosen_multiplier = recognized[0]["multiplier"] if unit_pass else np.nan
    vwap = raw_vwap * chosen_multiplier if unit_pass else raw_vwap * np.nan

    coverage = coverage_rows(panel, raw_eligible, amount_valid)
    annual = [row for row in coverage if row["dimension"] == "year"]
    overall_coverage = _fraction(np.count_nonzero(amount_valid),
                                 np.count_nonzero(raw_eligible))
    coverage_pass = bool(
        overall_coverage >= float(config["overall_amount_coverage_min"]) and
        all(row["coverage"] >= float(config["annual_amount_coverage_min"])
            for row in annual))

    adjustment_factor = np.divide(
        panel.close, panel.exec_close,
        out=np.full(panel.close.shape, np.nan, dtype=float),
        where=(np.isfinite(panel.close) & np.isfinite(panel.exec_close) &
               (panel.exec_close > 0)))
    adjusted_vwap = vwap * adjustment_factor
    adjusted_comparable, adjusted_inside = range_membership(
        adjusted_vwap, panel.low, panel.high,
        config["adjusted_range_tolerance_bps"])
    adjusted_valid = amount_valid & adjusted_comparable
    adjusted_fraction = _fraction(
        np.count_nonzero(adjusted_inside & amount_valid),
        np.count_nonzero(adjusted_valid))
    adjusted_pass = bool(
        np.isfinite(adjusted_fraction) and adjusted_fraction >=
        float(config["adjusted_inside_range_fraction_min"]))

    prior = adjustment_factor[:-1]
    current = adjustment_factor[1:]
    change = (np.isfinite(prior) & (prior > 0) & np.isfinite(current) &
              (current > 0) &
              (np.abs(np.log(current / prior)) >=
               float(config["adjustment_change_threshold"])))
    change_rows = np.zeros(panel.close.shape, dtype=bool)
    change_rows[1:] = change
    change_valid = change_rows & adjusted_valid
    change_fraction = _fraction(
        np.count_nonzero(change_rows & adjusted_inside),
        np.count_nonzero(change_valid))
    change_pass = bool(
        np.count_nonzero(change_valid) > 0 and
        change_fraction >=
        float(config["adjustment_change_inside_range_fraction_min"]))

    pd.DataFrame(candidates).to_csv(
        args.output_dir / "unit_candidates.csv", index=False)
    pd.DataFrame(coverage).to_csv(
        args.output_dir / "coverage.csv", index=False)
    gates = {
        "unique_unit_multiplier": "PASS" if unit_pass else "FAIL",
        "overall_and_annual_coverage": "PASS" if coverage_pass else "FAIL",
        "adjusted_range_mapping": "PASS" if adjusted_pass else "FAIL",
        "adjustment_change_mapping": "PASS" if change_pass else "FAIL",
    }
    passed = all(value == "PASS" for value in gates.values())
    report = {
        "audit_id": config["audit_id"],
        "role": config["role"],
        "rows": {
            "raw_eligible": int(np.count_nonzero(raw_eligible)),
            "amount_available": int(np.count_nonzero(amount_valid)),
            "adjustment_change_comparable": int(np.count_nonzero(change_valid)),
        },
        "unit_candidates": candidates,
        "recognized_unit_multiplier": chosen_multiplier if unit_pass else None,
        "overall_amount_coverage": overall_coverage,
        "minimum_annual_amount_coverage": float(min(
            row["coverage"] for row in annual)),
        "adjusted_inside_range_fraction": adjusted_fraction,
        "adjustment_change_inside_range_fraction": change_fraction,
        "gates": gates,
        "decision": "SCALE_AUDIT_PASS" if passed else "VWAP_FACTOR_BLOCKED",
        "factor_admitted": False,
        "missing_amount_policy": config["missing_amount_policy"],
        "warning": (
            "A scale-audit pass only permits a separately registered VWAP "
            "factor experiment; it does not establish predictive value."),
    }
    write_json(args.output_dir / "report.json", report)
    write_json(args.output_dir / "completion.json", {
        "status": "COMPLETE", "decision": report["decision"],
        "report_sha256": canonical_hash(report),
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
