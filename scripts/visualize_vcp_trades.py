#!/usr/bin/env python3
"""Generate per-trade annotated K-line charts from an audited strategy run."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuTradeVisualization import generate_trade_report  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--adjusted-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--raw-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research/raw"))
    parser.add_argument("--security-master", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research/security_master.csv"))
    parser.add_argument("--symbol", help="only render one symbol, e.g. sz000651")
    parser.add_argument("--limit", type=int, help="render only the first N trades")
    parser.add_argument("--before", type=int, default=30,
                        help="sessions shown before the entry signal")
    parser.add_argument("--after", type=int, default=10,
                        help="sessions shown after the sell fill")
    parser.add_argument("--title", help="HTML report title")
    parser.add_argument("--allow-open", action="store_true",
                        help="render closed trades while leaving open buys out")
    args = parser.parse_args()
    trades, index = generate_trade_report(
        args.backtest_dir, args.adjusted_dir, args.raw_dir, args.output_dir,
        security_master=args.security_master, symbol=args.symbol,
        limit=args.limit, before=args.before, after=args.after,
        title=args.title, require_all_closed=not args.allow_open,
    )
    print("rendered {} round trips".format(len(trades)))
    print(index)


if __name__ == "__main__":
    main()
