#!/usr/bin/env python3
"""Audit immutable M6 forward captures and their as-of eligibility."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuShortLineEvents import (  # noqa: E402
    ImmutableShortLineSnapshotStore, load_shortline_forward_policy,
    read_forward_anchor, stable_payload_hash,
)


DEFAULT_FORWARD_CONFIG = (
    ROOT / "configs/selection/shortline_forward_v1.json")


def audit(root, forward_config=None):
    root = Path(root)
    policy = load_shortline_forward_policy(
        forward_config or DEFAULT_FORWARD_CONFIG)
    store = ImmutableShortLineSnapshotStore(root)
    rows, violations = [], []
    for meta in store.iter_metadata():
        raw_path = root / meta["raw_path"]
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            payload_matches = stable_payload_hash(raw["records"]) == meta["payload_sha256"]
        except Exception as error:
            payload_matches = False
            violations.append("unreadable raw {}: {}".format(raw_path, error))
        normalized_exists = (not meta.get("normalized_path") or
                             (root / meta["normalized_path"]).exists())
        eligibility_valid = not (
            meta.get("asof_feature_allowed") and
            (meta.get("availability_evidence") != "FORWARD_CAPTURE" or
             meta.get("status") != "success" or
             meta.get("missing_required_columns")))
        strategy_eligibility_valid = not (
            meta.get("strategy_feature_allowed") and
            not meta.get("asof_feature_allowed"))
        if not payload_matches:
            violations.append("payload hash mismatch: {}".format(raw_path))
        if not normalized_exists:
            violations.append("missing normalized target: {}".format(
                meta.get("normalized_path")))
        if not eligibility_valid:
            violations.append("invalid as-of eligibility: {}".format(raw_path))
        if not strategy_eligibility_valid:
            violations.append("invalid strategy eligibility: {}".format(raw_path))
        rows.append({
            "trade_date": meta["query_date"], "dataset": meta["dataset"],
            "status": meta["status"],
            "availability_evidence": meta["availability_evidence"],
            "asof_feature_allowed": meta["asof_feature_allowed"],
            "strategy_feature_allowed": meta.get(
                "strategy_feature_allowed", False),
            "row_count": meta["row_count"],
            "normalization_status": meta["normalization_status"],
            "payload_matches": payload_matches,
            "normalized_exists": normalized_exists,
            "eligibility_valid": eligibility_valid,
            "strategy_eligibility_valid": strategy_eligibility_valid,
        })
    frame = pd.DataFrame(rows)
    eligible_dates = sorted(frame.loc[
        frame.asof_feature_allowed, "trade_date"].astype(int).unique()) if len(frame) else []
    run_rows = []
    for path in sorted((root / "_runs").glob("*/*.json")):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            violations.append("unreadable run {}: {}".format(path, error))
            continue
        if run.get("collector_version") != "akshare_shortline_forward_v1":
            continue
        trade_date = int(run["trade_date"])
        governed_run = bool(run.get("forward_policy_version")) or (
            trade_date >= policy.nominal_start_date)
        if run.get("feature_mode") != "shadow_only":
            violations.append("run is not shadow-only: {}".format(path))
        if governed_run and run.get("order_mutation_allowed") is not False:
            violations.append("run permits order mutation: {}".format(path))
        if (run.get("forward_policy_sha256") and
                run.get("forward_policy_sha256") != policy.sha256):
            violations.append("run policy hash mismatch: {}".format(path))
        sample_eligible = bool(run.get("forward_sample_eligible"))
        if sample_eligible and trade_date < policy.nominal_start_date:
            violations.append("prestart run marked forward eligible: {}".format(path))
        if sample_eligible and not run.get("forward_archive_complete"):
            violations.append("incomplete run marked forward eligible: {}".format(path))
        run_rows.append({
            "trade_date": trade_date,
            "path": str(path.relative_to(root)),
            "status": run.get("status"),
            "forward_archive_complete": bool(
                run.get("forward_archive_complete")),
            "forward_sample_eligible": sample_eligible,
            "forward_sample_status": run.get("forward_sample_status", ""),
        })
    forward_dates = sorted({
        row["trade_date"] for row in run_rows
        if row["forward_sample_eligible"]
    })
    try:
        anchor = read_forward_anchor(root, policy)
    except (OSError, ValueError) as error:
        anchor = None
        violations.append("invalid forward anchor: {}".format(error))
    anchor_date = int(anchor["anchor_trade_date"]) if anchor else None
    if forward_dates and anchor_date != forward_dates[0]:
        violations.append(
            "forward anchor does not equal first eligible session")
    if anchor_date is not None and not forward_dates:
        violations.append("forward anchor has no eligible run evidence")
    return frame, {
        "audit_version": "shortline_forward_audit_v1",
        "status": "failed" if violations else
                  ("passed" if rows else "empty"),
        "capture_count": len(rows), "eligible_dates": eligible_dates,
        "eligible_session_count": len(eligible_dates),
        "forward_policy_version": policy.policy_version,
        "forward_policy_sha256": policy.sha256,
        "forward_nominal_start_date": policy.nominal_start_date,
        "forward_anchor_trade_date": anchor_date,
        "forward_eligible_dates": forward_dates,
        "forward_eligible_session_count": len(forward_dates),
        "feature_mode": policy.feature_mode,
        "order_mutation_allowed": policy.order_mutation_allowed,
        "violations": violations,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research/shortline_forward"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--forward-config", type=Path,
                        default=DEFAULT_FORWARD_CONFIG)
    args = parser.parse_args()
    frame, report = audit(args.input_dir, args.forward_config)
    output = args.output_dir or (args.input_dir / "_audit")
    output.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output / "capture_audit.csv", index=False)
    (output / "audit_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    report["report_sha256"] = hashlib.sha256(
        (output / "audit_report.json").read_bytes()).hexdigest()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
