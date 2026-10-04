#!/usr/bin/env python3
"""Collect normalized minute bars into the immutable shadow store."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from abupy.MarketBu.ABuMinuteBarStore import (  # noqa: E402
    MinuteBarStore, minute_events_from_frame,
)
from abupy.MarketBu.ABuRealtimeMarket import (  # noqa: E402
    AKShareRealtimeMarketData,
)


def collect(adapter, store, symbols, period="1", start=None, end=None):
    results = []
    for symbol in sorted(set(symbols)):
        frame = adapter.minute_bars(
            symbol, period=period, start=start, end=end, adjust="")
        events = minute_events_from_frame(frame)
        results.extend(store.append(events))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True)
    parser.add_argument("--symbols", required=True,
                        help="comma-separated exchange-prefixed symbols")
    parser.add_argument("--period", default="1")
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args(argv)
    results = collect(
        AKShareRealtimeMarketData(), MinuteBarStore(args.store),
        [item.strip() for item in args.symbols.split(",") if item.strip()],
        period=args.period, start=args.start, end=args.end)
    print(json.dumps(results, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
