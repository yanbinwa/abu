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
    ImmutableShortLineSnapshotStore, stable_payload_hash,
)


def audit(root):
    root = Path(root)
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
    return frame, {
        "audit_version": "shortline_forward_audit_v1",
        "status": "passed" if rows and not violations else
                  ("empty" if not rows else "failed"),
        "capture_count": len(rows), "eligible_dates": eligible_dates,
        "eligible_session_count": len(eligible_dates),
        "violations": violations,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research/shortline_forward"))
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    frame, report = audit(args.input_dir)
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
