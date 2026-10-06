#!/usr/bin/env python3
"""Download resumable retrospective eltdx close-event batches.

The resulting history is deliberately labelled BACKFILLED_QUERY and may only
be used for historical hypothesis screening.  It is never promoted to the
forward short-line archive or treated as strict point-in-time evidence.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SHANGHAI = ZoneInfo("Asia/Shanghai")
DATASET_VERSION = "eltdx_close_history_backfill_v1"
PARSED_FIELDS = (
    "trading_date_value", "code", "full_code", "name", "status",
    "board_level", "highest_board_level", "industry", "limit_reason",
    "limit_reason_extra", "seal_amount", "limit_time", "broken_count",
)


def month_windows(start_date, end_date):
    start = pd.Timestamp(str(int(start_date)))
    end = pd.Timestamp(str(int(end_date)))
    if start > end:
        raise ValueError("start_date must not exceed end_date")
    rows = []
    for period in pd.period_range(start, end, freq="M"):
        first = max(start, period.start_time)
        last = min(end, period.end_time.normalize())
        rows.append((int(first.strftime("%Y%m%d")),
                     int(last.strftime("%Y%m%d"))))
    return rows


def _json_bytes(payload):
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), default=str) + "\n").encode()


def _atomic_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _atomic_gzip_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as output:
            output.write(_json_bytes(payload))
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(temporary, path)


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _parsed_rows(result):
    return [
        {field: getattr(row, field, None) for field in PARSED_FIELDS}
        for row in getattr(result, "rows", ())
    ]


def _raw_rows(result):
    return [dict(getattr(row, "raw", {}) or {})
            for row in getattr(result, "rows", ())]


def _retry(call, attempts, sleep_seconds, sleep_fn=time.sleep):
    last = None
    for attempt in range(1, int(attempts) + 1):
        try:
            return call()
        except Exception as error:
            last = error
            if attempt < int(attempts):
                sleep_fn(float(sleep_seconds) * attempt)
    raise last


def _complete_manifest(month_root, expected_sessions=None):
    candidates = []
    for path in sorted(Path(month_root).glob("*/manifest.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        requested = set(int(value) for value in (expected_sessions or ()))
        archived = set(int(value) for value in
                       payload.get("expected_sessions", ()))
        covers_request = requested.issubset(archived)
        if payload.get("status") == "complete" and covers_request:
            candidates.append((path, payload))
    return candidates[-1] if candidates else None


def download_history(*, client, calendar_dates, output_dir, start_date,
                     end_date, attempts=4, sleep_seconds=1.0,
                     max_months=None, sleep_fn=time.sleep, now_fn=None,
                     progress=None, chunk_sessions=None):
    """Download immutable monthly batches and return a compact run summary."""
    now_fn = now_fn or (lambda: datetime.now(SHANGHAI))
    output_dir = Path(output_dir)
    calendar = sorted({int(value) for value in calendar_dates
                       if int(start_date) <= int(value) <= int(end_date)})
    windows = month_windows(start_date, end_date)
    if max_months is not None:
        windows = windows[:int(max_months)]
    run = {
        "dataset_version": DATASET_VERSION,
        "started_at": now_fn().isoformat(),
        "start_date": int(start_date), "end_date": int(end_date),
        "availability_evidence": "BACKFILLED_QUERY",
        "strict_pit_allowed": False,
        "months": [],
    }
    for query_start, query_end in windows:
        month = str(query_start)[:6]
        month_root = output_dir / "batches" / month
        expected = [value for value in calendar
                    if query_start <= value <= query_end]
        existing = _complete_manifest(month_root, expected)
        if existing is not None:
            run["months"].append({
                "month": month, "status": "skipped_complete",
                "manifest": str(existing[0]),
                "row_count": int(existing[1]["row_count"]),
            })
            if progress is not None:
                progress(run["months"][-1])
            continue
        stamp = now_fn().strftime("%Y%m%dT%H%M%S%f%z")
        batch_id = "{}_{}".format(stamp, uuid.uuid4().hex[:8])
        batch_dir = month_root / batch_id
        batch_dir.mkdir(parents=True, exist_ok=False)
        try:
            if chunk_sessions:
                size = int(chunk_sessions)
                ranges = [(chunk[0], chunk[-1]) for chunk in
                          (expected[index:index + size]
                           for index in range(0, len(expected), size))
                          if chunk]
            else:
                ranges = [(query_start, query_end)]
            parsed, raw, request_bodies = [], [], []
            for range_start, range_end in ranges:
                result = _retry(
                    lambda range_start=range_start, range_end=range_end:
                    client.limit_up_down_list(
                        str(range_start), str(range_end)),
                    attempts, sleep_seconds, sleep_fn)
                parsed.extend(_parsed_rows(result))
                raw.extend(_raw_rows(result))
                request_bodies.append(getattr(result, "request_body", None))
                sleep_fn(float(sleep_seconds))
            observed = {
                int(item["trading_date_value"]) for item in parsed
                if item.get("trading_date_value") not in (None, "")}
            missing = sorted(set(expected) - observed)
            # A missing date cannot be assumed to mean zero events.  Query it
            # separately and only accept the month when every session appears.
            for value in missing:
                supplement = _retry(
                    lambda value=value: client.limit_up_down_list(str(value)),
                    attempts, sleep_seconds, sleep_fn)
                parsed.extend(_parsed_rows(supplement))
                raw.extend(_raw_rows(supplement))
                request_bodies.append(getattr(
                    supplement, "request_body", None))
                if any(int(item["trading_date_value"]) == value
                       for item in _parsed_rows(supplement)
                       if item.get("trading_date_value") not in (None, "")):
                    observed.add(value)
                sleep_fn(float(sleep_seconds))
            missing = sorted(set(expected) - observed)
            parsed_frame = pd.DataFrame(parsed, columns=PARSED_FIELDS)
            if len(parsed_frame):
                parsed_frame = parsed_frame.drop_duplicates(
                    ["trading_date_value", "code", "status"], keep="last")
                parsed_frame = parsed_frame.sort_values(
                    ["trading_date_value", "status", "code"],
                    kind="mergesort")
            raw_path = batch_dir / "provider_rows.json.gz"
            parsed_path = batch_dir / "parsed_events.csv.gz"
            _atomic_gzip_json(raw_path, {
                "dataset_version": DATASET_VERSION,
                "query_start": query_start, "query_end": query_end,
                "request_bodies": request_bodies,
                "records": raw,
            })
            parsed_frame.to_csv(
                parsed_path, index=False,
                compression={"method": "gzip", "compresslevel": 3,
                             "mtime": 0})
            status = "complete" if expected and not missing else "incomplete"
            manifest = {
                "dataset_version": DATASET_VERSION,
                "batch_id": batch_id, "month": month,
                "query_start": query_start, "query_end": query_end,
                "captured_at": now_fn().isoformat(),
                "availability_evidence": "BACKFILLED_QUERY",
                "strict_pit_allowed": False,
                "status": status,
                "expected_sessions": expected,
                "observed_sessions": sorted(observed),
                "missing_sessions": missing,
                "row_count": int(len(parsed_frame)),
                "query_chunk_sessions": (
                    int(chunk_sessions) if chunk_sessions else None),
                "raw_path": str(raw_path),
                "raw_sha256": _sha256(raw_path),
                "parsed_path": str(parsed_path),
                "parsed_sha256": _sha256(parsed_path),
            }
        except Exception as error:
            manifest = {
                "dataset_version": DATASET_VERSION,
                "batch_id": batch_id, "month": month,
                "query_start": query_start, "query_end": query_end,
                "captured_at": now_fn().isoformat(),
                "availability_evidence": "BACKFILLED_QUERY",
                "strict_pit_allowed": False, "status": "error",
                "expected_sessions": expected,
                "observed_sessions": [], "missing_sessions": expected,
                "row_count": 0,
                "error_code": "SOURCE_REQUEST_FAILED",
                "error_message": "{}: {}".format(
                    type(error).__name__, str(error)[:500]),
            }
        _atomic_bytes(batch_dir / "manifest.json", _json_bytes(manifest))
        run["months"].append({
            "month": month, "status": manifest["status"],
            "manifest": str(batch_dir / "manifest.json"),
            "row_count": int(manifest["row_count"]),
            "missing_sessions": manifest["missing_sessions"],
        })
        if progress is not None:
            progress(run["months"][-1])
        sleep_fn(float(sleep_seconds))
    counts = pd.Series([item["status"] for item in run["months"]]).value_counts()
    run["status_counts"] = {str(key): int(value) for key, value in counts.items()}
    run["completed_at"] = now_fn().isoformat()
    run_dir = output_dir / "_runs"
    run_name = run["started_at"].replace(":", "").replace("+", "_")
    _atomic_bytes(run_dir / (run_name + ".json"), _json_bytes(run))
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=int, default=20200101)
    parser.add_argument("--end-date", type=int, default=20241231)
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research/eltdx_close_history_v1"))
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--max-months", type=int)
    parser.add_argument("--chunk-sessions", type=int, default=5,
                        help="query smaller trading-session ranges; 0 uses a month")
    args = parser.parse_args()
    import akshare as ak
    from eltdx import F10Client

    calendar_frame = ak.tool_trade_date_hist_sina()
    calendar = pd.to_datetime(calendar_frame["trade_date"]).dt.strftime(
        "%Y%m%d").astype(int)
    result = download_history(
        client=F10Client(timeout=float(args.timeout)),
        calendar_dates=calendar, output_dir=args.output_dir,
        start_date=args.start_date, end_date=args.end_date,
        attempts=args.attempts, sleep_seconds=args.sleep_seconds,
        max_months=args.max_months,
        chunk_sessions=(args.chunk_sessions or None),
        progress=lambda item: print(json.dumps(
            item, ensure_ascii=False), flush=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    failed = sum(value for key, value in result["status_counts"].items()
                 if key not in ("complete", "skipped_complete"))
    if failed:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
