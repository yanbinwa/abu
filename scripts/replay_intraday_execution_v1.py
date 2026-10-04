#!/usr/bin/env python3
"""Replay serialized approved orders and minute events with D0/M1/M2."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from abupy.AlphaBu.ABuMinuteReplay import (  # noqa: E402
    FixedDelayModel, run_paired_replay, write_paired_replay,
)
from abupy.AlphaBu.ABuTradeIntent import ApprovedOrder  # noqa: E402
from abupy.MarketBu.ABuRealtimeMarket import MinuteBarEvent  # noqa: E402


def _jsonl(path, cls):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line:
            payload = json.loads(line)
            if cls is MinuteBarEvent:
                payload["quality_codes"] = tuple(payload.get("quality_codes", ()))
            rows.append(cls(**payload))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders", required=True)
    parser.add_argument("--bars", required=True)
    parser.add_argument("--d0-fills", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--delay-ms", type=int, required=True)
    parser.add_argument("--source-snapshot-hash", required=True)
    parser.add_argument("--data-manifest-hash", required=True)
    args = parser.parse_args(argv)
    orders = _jsonl(args.orders, ApprovedOrder)
    bars = _jsonl(args.bars, MinuteBarEvent)
    by_symbol = {}
    for item in bars:
        by_symbol.setdefault(item.symbol, []).append(item)
    d0 = {
        item["order_id"]: item for item in
        json.loads(Path(args.d0_fills).read_text(encoding="utf-8"))
    }
    records, manifest = run_paired_replay(
        orders, by_symbol, d0,
        latency_model=FixedDelayModel(args.delay_ms),
        source_snapshot_hash=args.source_snapshot_hash,
        data_manifest_hash=args.data_manifest_hash)
    write_paired_replay(args.output, records, manifest)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
