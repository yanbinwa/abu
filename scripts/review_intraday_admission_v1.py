#!/usr/bin/env python3
"""Evaluate frozen software and observation evidence for v1 admission."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from abupy.AlphaBu.ABuIntradayAdmission import (  # noqa: E402
    AdmissionEvidence, evaluate_admission, write_admission_report,
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    evidence = AdmissionEvidence(**json.loads(
        Path(args.evidence).read_text(encoding="utf-8")))
    report = evaluate_admission(evidence)
    write_admission_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
