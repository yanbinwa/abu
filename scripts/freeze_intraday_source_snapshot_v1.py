#!/usr/bin/env python3
"""Freeze the exact source bytes used by an intraday execution run."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from abupy.AlphaBu.ABuSourceSnapshot import (
    build_source_snapshot, write_source_snapshot,
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=REPO_ROOT)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    snapshot = build_source_snapshot(args.repo_root)
    write_source_snapshot(args.output, snapshot)
    print(json.dumps({
        "output": str(Path(args.output).resolve()),
        "dirty": snapshot["dirty"],
        "source_snapshot_hash": snapshot["source_snapshot_hash"],
        "file_count": len(snapshot["files"]),
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
