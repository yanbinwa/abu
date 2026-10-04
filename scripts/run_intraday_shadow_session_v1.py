#!/usr/bin/env python3
"""Collect a full A-share session and run broker-free M1/M2 decisions."""
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
from abupy.MarketBu.ABuMinuteBarStore import (  # noqa: E402
    MinuteBarStore, minute_events_from_frame,
)
from abupy.MarketBu.ABuRealtimeMarket import (  # noqa: E402
    AKShareRealtimeMarketData, RealtimeMarketDataError,
    normalize_cn_symbol,
)


DEFAULT_OBSERVATION_SYMBOLS = ("sh600519", "sz000001", "sz300750")


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


def load_watchlist(paper_state_path, orders, extra_symbols=()):
    """Freeze orders, holdings and explicit sentinels into a bounded watchlist."""
    payload = json.loads(Path(paper_state_path).read_text(encoding="utf-8"))
    symbols = {item.symbol for item in orders}
    symbols.update(payload.get("active", {}).get("positions", {}).keys())
    symbols.update(extra_symbols)
    return sorted(normalize_cn_symbol(item) for item in symbols if item)


def prepare_session(run_root, paper_state_path, source_snapshot_hash,
                    trade_date, created_at, extra_symbols=()):
    session = Path(run_root) / "sessions" / str(trade_date)
    session.mkdir(parents=True, exist_ok=True)
    orders = load_session_orders(paper_state_path, trade_date)
    watchlist = load_watchlist(paper_state_path, orders, extra_symbols)
    if not watchlist:
        raise ValueError("full-session collection requires a non-empty watchlist")
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
        "watchlist": watchlist,
        "effective_order_day": bool(orders),
        "status": "READY" if orders else "COLLECT_ONLY_NO_PENDING_BUYS",
        "created_at": created_at,
    }
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        immutable = ("trade_date", "paper_state_sha256", "source_snapshot_hash",
                     "order_ids", "watchlist")
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


def collect_watchlist_once(adapter, store, symbols, trade_date, now):
    date = str(trade_date)
    day = "{}-{}-{}".format(date[:4], date[4:6], date[6:])
    rows = []
    for symbol in symbols:
        try:
            frame = adapter.minute_bars(
                symbol, period="1", start=day + " 09:30:00",
                end=now.strftime("%Y-%m-%d %H:%M:%S"), adjust="")
            events = minute_events_from_frame(frame)
            appended = store.append(events)
            health = adapter.health(symbol) or adapter.health()
            rows.append({
                "symbol": symbol, "status": "OK",
                "appended": sum(item["appended"] for item in appended),
                "health": health.to_dict(),
            })
        except RealtimeMarketDataError as error:
            health = adapter.health(symbol) or adapter.health()
            rows.append({
                "symbol": symbol, "status": "ERROR", "error": str(error),
                "appended": 0, "health": health.to_dict(),
            })
    return rows


def _write_health_batch(session, now, health_rows):
    payload = {
        "schema_version": "intraday_market_health_v1",
        "polled_at": now.isoformat(),
        "symbols": health_rows,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    path = Path(session) / "market_health" / "{}.json".format(digest)
    if not path.exists():
        _atomic_json(path, payload)
    return path


def poll_session(session, minute_store, watchlist, trade_date, now=None,
                 adapter=None):
    now = now or (lambda: pd.Timestamp.now(tz="Asia/Shanghai"))
    polled_at = pd.Timestamp(now()).tz_convert("Asia/Shanghai")
    store = MinuteBarStore(minute_store)
    adapter = adapter or AKShareRealtimeMarketData(
        raw_archive=store.append_raw_response)
    if hasattr(adapter, "raw_archive") and adapter.raw_archive is None:
        adapter.raw_archive = store.append_raw_response
    health_rows = collect_watchlist_once(
        adapter, store, watchlist, trade_date, polled_at)
    _write_health_batch(session, polled_at, health_rows)
    health_by_symbol = {item["symbol"]: item for item in health_rows}
    results = {}
    for policy in ("M1", "M2"):
        if not (Path(session) / policy / "state.json").exists():
            continue
        state = IntradayShadowRunner(
            Path(session) / policy, adapter, store,
            now=lambda: polled_at).poll_once(
                prefetched_health=health_by_symbol)
        results[policy] = {
            "outcomes": state["outcomes"],
            "poll_count": len(state["polls"]),
        }
    return {"policies": results, "market_health": health_rows}


def _is_collection_time(now, start_at, morning_end, afternoon_start, end_at):
    return ((start_at <= now <= morning_end) or
            (afternoon_start <= now <= end_at))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--paper-state", required=True)
    parser.add_argument("--source-snapshot-hash", required=True)
    parser.add_argument("--trade-date", type=int)
    parser.add_argument("--start-at", default="09:31:05")
    parser.add_argument("--morning-end-at", default="11:30:30")
    parser.add_argument("--afternoon-start-at", default="13:01:05")
    parser.add_argument("--end-at", default="15:01:30")
    parser.add_argument("--poll-interval-seconds", type=float, default=60.0)
    parser.add_argument(
        "--extra-symbols", default=",".join(DEFAULT_OBSERVATION_SYMBOLS),
        help="comma-separated sentinels collected even without orders")
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
            trade_date, now.isoformat(), extra_symbols=[
                item.strip() for item in args.extra_symbols.split(",")
                if item.strip()])
        start_at = _clock_on(trade_date, args.start_at)
        morning_end = _clock_on(trade_date, args.morning_end_at)
        afternoon_start = _clock_on(trade_date, args.afternoon_start_at)
        end_at = _clock_on(trade_date, args.end_at)
        if not start_at < morning_end < afternoon_start < end_at:
            parser.error("collection session clocks must be strictly ordered")
        polls = 0
        latest = {}
        adapter = AKShareRealtimeMarketData()
        while True:
            current = pd.Timestamp.now(tz="Asia/Shanghai")
            if not args.once and current > end_at:
                break
            if not args.once and not _is_collection_time(
                    current, start_at, morning_end, afternoon_start, end_at):
                next_start = start_at if current < start_at else afternoon_start
                time.sleep(min(args.poll_interval_seconds,
                               max(0.0, (next_start - current).total_seconds())))
                continue
            cycle_started = time.monotonic()
            latest = poll_session(
                session, run_root / "minute_store", manifest["watchlist"],
                trade_date, adapter=adapter)
            polls += 1
            if args.once:
                break
            time.sleep(max(
                0.0, args.poll_interval_seconds -
                (time.monotonic() - cycle_started)))
        finished = dict(manifest)
        finished.update({
            "status": ("COLLECTION_COMPLETE" if polls
                       else "COLLECTION_MISSED"),
            "completed_at": datetime.now().astimezone().isoformat(),
            "session_poll_cycles": polls,
            "policy_results": latest.get("policies", {}),
            "last_market_health": latest.get("market_health", []),
        })
        _atomic_json(session / "session_result.json", finished)
        print(json.dumps(finished, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
