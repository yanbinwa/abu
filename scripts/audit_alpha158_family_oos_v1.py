#!/usr/bin/env python3
"""Audit long-history seven-family OOS predictions without changing them."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.MLBu.ABuMLFamilyOOSAudit import audit_family_set  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_history_2015_validation_v1_20261007"))
    parser.add_argument("--dataset-report", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research_2015_v1/dataset_report.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit_family_set(args.root, args.dataset_report)
    destination = args.output or args.root/"ml_family_oos_audit.json"
    destination.write_text(json.dumps(
        result, ensure_ascii=False, indent=2, sort_keys=True)+"\n",
        encoding="utf-8")
    print(json.dumps({
        "status": result["status"],
        "incomplete_families": result["incomplete_families"],
        "output": str(destination)}, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

