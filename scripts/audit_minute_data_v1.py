#!/usr/bin/env python3
"""Audit one immutable minute-bar partition."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from abupy.MarketBu.ABuMinuteBarStore import MinuteBarStore  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--period", type=int, default=1)
    parser.add_argument("--source")
    args = parser.parse_args(argv)
    result = MinuteBarStore(args.store).audit(
        args.symbol, args.trade_date, args.period, source=args.source)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["healthy"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
