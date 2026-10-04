#!/usr/bin/env python3
"""Replay A-share capital events from immutable CNINFO raw responses."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuFundamentalPIT import (  # noqa: E402
    stable_json, structured_share_events,
)
from scripts.collect_structured_fundamental_v4 import trading_sessions  # noqa: E402


DEFAULT_CONFIG = (
    ROOT / "configs/selection/fundamental_structured_pit_v4.json"
)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def _eligible_batches(raw_root, symbols, retrieved_on=None):
    grouped = defaultdict(list)
    for path in Path(raw_root).glob("batches/**/*.json"):
        try:
            metadata = _read_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if metadata.get("source") != "cninfo_share_change" or \
                metadata.get("dataset") != "shares":
            continue
        symbol = metadata.get("symbol")
        if symbol not in symbols:
            continue
        if retrieved_on and not str(metadata.get("retrieved_at_utc", "")).startswith(
                retrieved_on):
            continue
        if metadata.get("outcome") not in {
                "SUCCESS_NONEMPTY", "SUCCESS_EMPTY"}:
            continue
        grouped[symbol].append((metadata, path))
    selected = {}
    for symbol, items in grouped.items():
        selected[symbol] = sorted(items, key=lambda item: (
            str(item[0].get("retrieved_at_utc", "")),
            str(item[0].get("batch_id", "")),
        ))[-1]
    return selected


def replay(pilot_dir, raw_root, output_dir, sessions, retrieved_on=None):
    pilot_dir = Path(pilot_dir)
    raw_root = Path(raw_root)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite share replay")
    symbols = _read_json(pilot_dir / "pilot_symbols.json")["symbols"]
    selected = _eligible_batches(raw_root, set(symbols), retrieved_on)
    events = []
    symbol_reports = {}
    failures = []

    for symbol in symbols:
        if symbol not in selected:
            failures.append({
                "symbol": symbol, "reason": "NO_ELIGIBLE_RAW_BATCH"})
            continue
        metadata, metadata_path = selected[symbol]
        try:
            payload_hash = str(metadata["payload_sha256"])
            blob = raw_root / str(metadata["blob_path"])
            payload = blob.read_bytes()
            if _sha256(payload) != payload_hash:
                raise ValueError("PAYLOAD_HASH_MISMATCH")
            body = json.loads(payload) if payload else {}
            rows = body.get("records") or []
            normalized, audit = structured_share_events(
                rows, symbol, payload_hash, sessions)
            events.extend(event.__dict__ for event in normalized)
            symbol_reports[symbol] = {
                "batch_id": metadata.get("batch_id"),
                "metadata_path": str(metadata_path),
                "payload_sha256": payload_hash,
                "retrieved_at_utc": metadata.get("retrieved_at_utc"),
                "outcome": metadata.get("outcome"),
                **audit,
            }
        except (KeyError, OSError, TypeError, ValueError) as error:
            failures.append({
                "symbol": symbol, "reason": type(error).__name__,
                "detail": str(error),
                "batch_id": metadata.get("batch_id"),
            })

    output_dir.mkdir(parents=True)
    events.sort(key=lambda item: (
        item["symbol"], item["effective_at"], item["source_key"]))
    events_path = output_dir / "share_events.jsonl"
    report_path = output_dir / "replay_report.json"
    events_path.write_text("".join(
        stable_json(item) + "\n" for item in events), encoding="utf-8")
    missing = sorted(set(symbols) - {
        event["symbol"] for event in events})
    report = {
        "pilot_dir": str(pilot_dir),
        "raw_root": str(raw_root),
        "retrieved_on_filter": retrieved_on,
        "symbol_count": len(symbols),
        "selected_batch_count": len(selected),
        "normalized_event_count": len(events),
        "symbols_with_events": len(symbols) - len(missing),
        "symbols_without_events": missing,
        "symbol_reports": symbol_reports,
        "failures": failures,
        "float_share_field": "F022N",
        "float_share_basis": "circulating_a_shares",
        "status": "PASS_SHARE_REPLAY" if not failures else
        "BLOCKED_SHARE_REPLAY",
    }
    report_path.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return report, {"events": events_path, "report": report_path}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pilot_dir", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--retrieved-on")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    config = _read_json(args.config)
    raw_root = args.raw_root or Path(config["raw_root"])
    report, paths = replay(
        args.pilot_dir, raw_root, args.output_dir,
        trading_sessions(config["trading_sessions_source"]),
        retrieved_on=args.retrieved_on)
    console_report = {
        key: value for key, value in report.items()
        if key != "symbol_reports"
    }
    print(json.dumps({**console_report, "outputs": {
        key: str(value) for key, value in paths.items()
    }}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS_SHARE_REPLAY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
