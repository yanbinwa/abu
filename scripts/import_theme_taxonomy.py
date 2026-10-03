#!/usr/bin/env python3
"""Validate and publish one immutable bitemporal theme-taxonomy snapshot."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuShortLineEvents import ThemeTaxonomyStore  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research/theme_taxonomy"))
    args = parser.parse_args()
    frame = pd.read_csv(args.input, dtype=str)
    missing = set(ThemeTaxonomyStore.REQUIRED_COLUMNS) - set(frame.columns)
    if missing:
        raise SystemExit("taxonomy fields missing: {}".format(sorted(missing)))
    versions = sorted(frame.taxonomy_version.dropna().unique())
    if len(versions) != 1:
        raise SystemExit("one snapshot must contain exactly one taxonomy_version")
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%dT%H%M%S%f%z")
    output = args.output_dir / versions[0] / (stamp + ".csv")
    digest = ThemeTaxonomyStore.write_snapshot(
        output, frame[list(ThemeTaxonomyStore.REQUIRED_COLUMNS)].to_dict("records"))
    print(json.dumps({
        "status": "published", "path": str(output), "sha256": digest,
        "rows": len(frame), "taxonomy_version": versions[0],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
