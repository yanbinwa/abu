#!/usr/bin/env python3
"""Initialize or poll a broker-free intraday shadow state."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from abupy.AlphaBu.ABuIntradayShadow import (  # noqa: E402
    IntradayShadowRunner, initialize_shadow_state,
)
from abupy.AlphaBu.ABuTradeIntent import ApprovedOrder  # noqa: E402
from abupy.MarketBu.ABuMinuteBarStore import MinuteBarStore  # noqa: E402
from abupy.MarketBu.ABuRealtimeMarket import (  # noqa: E402
    AKShareRealtimeMarketData,
)


def _orders(path):
    return [ApprovedOrder(**json.loads(line)) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line]


def main(argv=None):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser("init")
    initialize.add_argument("--state-dir", required=True)
    initialize.add_argument("--orders", required=True)
    initialize.add_argument("--policy", choices=("M1", "M2"), required=True)
    initialize.add_argument("--source-snapshot-hash", required=True)
    poll = subparsers.add_parser("poll")
    poll.add_argument("--state-dir", required=True)
    poll.add_argument("--minute-store", required=True)
    args = parser.parse_args(argv)
    if args.command == "init":
        state = initialize_shadow_state(
            args.state_dir, _orders(args.orders), args.policy,
            args.source_snapshot_hash)
    else:
        state = IntradayShadowRunner(
            args.state_dir, AKShareRealtimeMarketData(),
            MinuteBarStore(args.minute_store)).poll_once()
    print(json.dumps({
        "state_version": state["state_version"],
        "policy_id": state["policy_id"],
        "outcomes": state["outcomes"],
        "broker_connected": state["broker_connected"],
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
