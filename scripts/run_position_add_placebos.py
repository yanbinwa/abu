#!/usr/bin/env python3
"""Run the pre-registered 1,000-path matched position-add placebo."""
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

from abupy.AlphaBu.ABuPositionAddAnalytics import (
    partition_matchable_actual_ids, run_matched_add_placebos,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--actual-ids", type=Path, required=True)
    parser.add_argument("--paths", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20261004)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--drop-unmatched", action="store_true")
    args = parser.parse_args()
    candidates = pd.read_csv(args.candidates)
    actual_ids = [line.strip() for line in args.actual_ids.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    requested = len(actual_ids)
    matched, unmatched = partition_matchable_actual_ids(candidates, actual_ids)
    if unmatched and not args.drop_unmatched:
        raise ValueError("{} actual ADDs lack an exact PIT peer".format(
            len(unmatched)))
    selected = matched if args.drop_unmatched else actual_ids
    result = run_matched_add_placebos(
        candidates, selected, args.paths, args.seed)
    result.update({"requested_actual_adds": requested,
                   "matched_actual_adds": len(selected),
                   "unmatched_actual_adds": len(unmatched),
                   "match_coverage_fraction": (len(selected)/requested
                                               if requested else 0.0)})
    args.output_dir.mkdir(parents=True, exist_ok=True)
    np.save(args.output_dir/"placebo_distribution.npy", result.pop("distribution"))
    (args.output_dir/"unmatched_actual_ids.txt").write_text(
        "\n".join(unmatched)+("\n" if unmatched else ""), encoding="utf-8")
    (args.output_dir/"placebo_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    main()
