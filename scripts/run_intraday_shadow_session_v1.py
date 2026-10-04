#!/usr/bin/env python3
"""Run one broker-free M1/M2 shadow session from frozen paper orders."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

import pandas as pd

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


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.{}.tmp".format(path.name, os.getpid()))
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    os.replace(temporary, path)


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_session_orders(paper_state_path, trade_date):
    """Copy today's pending paper buys without mutating the paper account."""
    payload = json.loads(Path(paper_state_path).read_text(encoding="utf-8"))
    result = []
    for item in payload.get("active", {}).get("orders", []):
        order = ApprovedOrder(**item)
        if order.side != "buy" or int(order.valid_session) not in (0, trade_date):
            continue
        if int(order.valid_session) == 0:
            order = replace(order, valid_session=trade_date)
        result.append(order)
    return sorted(result, key=lambda item: item.order_id)


def prepare_session(run_root, paper_state_path, source_snapshot_hash,
                    trade_date, created_at):
    session = Path(run_root) / "sessions" / str(trade_date)
    session.mkdir(parents=True, exist_ok=True)
    orders = load_session_orders(paper_state_path, trade_date)
    manifest_path = session / "session_manifest.json"
    manifest = {
        "schema_version": "intraday_shadow_session_v1",
        "trade_date": int(trade_date),
        "research_only": True,
        "broker_connected": False,
        "paper_state_path": str(Path(paper_state_path).resolve()),
        "paper_state_sha256": _sha256_file(paper_state_path),
        "source_snapshot_hash": source_snapshot_hash,
        "order_count": len(orders),
        "order_ids": [item.order_id for item in orders],
        "status": "READY" if orders else "SKIPPED_NO_PENDING_BUYS",
        "created_at": created_at,
    }
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        immutable = ("trade_date", "paper_state_sha256", "source_snapshot_hash",
                     "order_ids")
        if any(existing.get(key) != manifest.get(key) for key in immutable):
            raise ValueError("session frozen inputs changed")
        manifest = existing
    else:
        _atomic_json(manifest_path, manifest)
    if not orders:
        return session, orders, manifest
    orders_path = session / "approved_orders.jsonl"
    content = "".join(
        json.dumps(asdict(item), ensure_ascii=False, sort_keys=True) + "\n"
        for item in orders)
    if orders_path.exists() and orders_path.read_text(encoding="utf-8") != content:
        raise ValueError("session approved orders changed")
    if not orders_path.exists():
        orders_path.write_text(content, encoding="utf-8")
    for policy in ("M1", "M2"):
        policy_dir = session / policy
        if not (policy_dir / "state.json").exists():
            initialize_shadow_state(
                policy_dir, orders, policy, source_snapshot_hash,
                created_at=created_at)
    return session, orders, manifest


def _clock_on(trade_date, clock_text):
    value = str(trade_date)
    return pd.Timestamp("{}-{}-{}T{}+08:00".format(
        value[:4], value[4:6], value[6:], clock_text))


def poll_session(session, minute_store, now=None):
    now = now or (lambda: pd.Timestamp.now(tz="Asia/Shanghai"))
    adapter = AKShareRealtimeMarketData()
    store = MinuteBarStore(minute_store)
    results = {}
    for policy in ("M1", "M2"):
        state = IntradayShadowRunner(
            Path(session) / policy, adapter, store, now=now).poll_once()
        results[policy] = {
            "outcomes": state["outcomes"],
            "poll_count": len(state["polls"]),
        }
    return results


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--paper-state", required=True)
    parser.add_argument("--source-snapshot-hash", required=True)
    parser.add_argument("--trade-date", type=int)
    parser.add_argument("--start-at", default="09:35:05")
    parser.add_argument("--end-at", default="10:31:30")
    parser.add_argument("--poll-interval-seconds", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.poll_interval_seconds <= 0:
        parser.error("--poll-interval-seconds must be positive")
    now = pd.Timestamp.now(tz="Asia/Shanghai")
    trade_date = args.trade_date or int(now.strftime("%Y%m%d"))
    run_root = Path(args.run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    lock = (run_root / ".session.lock").open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another intraday shadow session is already running")
    try:
        session, orders, manifest = prepare_session(
            run_root, args.paper_state, args.source_snapshot_hash,
            trade_date, now.isoformat())
        if not orders:
            print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            return 0
        start_at = _clock_on(trade_date, args.start_at)
        end_at = _clock_on(trade_date, args.end_at)
        polls = 0
        latest = {}
        while True:
            current = pd.Timestamp.now(tz="Asia/Shanghai")
            if not args.once and current < start_at:
                time.sleep(min(args.poll_interval_seconds,
                               max(0.0, (start_at - current).total_seconds())))
                continue
            latest = poll_session(session, run_root / "minute_store")
            polls += 1
            if args.once or current >= end_at:
                break
            time.sleep(args.poll_interval_seconds)
        finished = dict(manifest)
        finished.update({
            "status": "POLLING_COMPLETE",
            "completed_at": datetime.now().astimezone().isoformat(),
            "session_poll_cycles": polls,
            "policy_results": latest,
        })
        _atomic_json(session / "session_result.json", finished)
        print(json.dumps(finished, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
