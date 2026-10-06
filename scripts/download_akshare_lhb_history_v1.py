#!/usr/bin/env python3
"""Download immutable AKShare/Eastmoney Dragon-Tiger List history.

The provider exposes retrospective data without a historical revision chain.
Every batch is therefore labelled BACKFILLED_QUERY and may only be used for
hypothesis screening.  Provider-computed forward returns and narrative success
rates are retained only in the raw response and never enter parsed features.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


SHANGHAI = ZoneInfo("Asia/Shanghai")
DATASET_VERSION = "akshare_eastmoney_lhb_backfill_v1"
FORBIDDEN_DETAIL_COLUMNS = (
    "解读", "上榜后1日", "上榜后2日", "上榜后5日", "上榜后10日",
)
DETAIL_REQUIRED = (
    "代码", "上榜日", "龙虎榜净买额", "龙虎榜买入额", "龙虎榜卖出额",
    "龙虎榜成交额", "市场总成交额", "换手率", "流通市值", "上榜原因",
)
INSTITUTION_REQUIRED = (
    "代码", "上榜日期", "买方机构数", "卖方机构数", "机构买入总额",
    "机构卖出总额", "机构买入净额", "市场总成交额", "换手率", "流通市值",
    "上榜原因",
)


def month_windows(start_date, end_date):
    start = pd.Timestamp(str(int(start_date)))
    end = pd.Timestamp(str(int(end_date)))
    if start > end:
        raise ValueError("start_date must not exceed end_date")
    return [
        (int(max(start, period.start_time).strftime("%Y%m%d")),
         int(min(end, period.end_time.normalize()).strftime("%Y%m%d")))
        for period in pd.period_range(start, end, freq="M")
    ]


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


def _required(frame, columns, dataset):
    if frame.empty:
        return
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError("{} missing columns: {}".format(
            dataset, ",".join(sorted(missing))))


def _number(frame, name):
    return pd.to_numeric(frame[name], errors="coerce")


def normalize_detail(frame):
    _required(frame, DETAIL_REQUIRED, "lhb_detail")
    columns = [
        "trade_date", "code", "net_buy", "buy_amount", "sell_amount",
        "lhb_turnover", "market_turnover", "turnover_pct",
        "free_float_market_cap", "reason_raw",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    output = pd.DataFrame({
        "trade_date": pd.to_datetime(frame["上榜日"]).dt.strftime(
            "%Y%m%d").astype(int),
        "code": frame["代码"].astype(str).str.split(".").str[0].str.zfill(6),
        "net_buy": _number(frame, "龙虎榜净买额"),
        "buy_amount": _number(frame, "龙虎榜买入额"),
        "sell_amount": _number(frame, "龙虎榜卖出额"),
        "lhb_turnover": _number(frame, "龙虎榜成交额"),
        "market_turnover": _number(frame, "市场总成交额"),
        "turnover_pct": _number(frame, "换手率"),
        "free_float_market_cap": _number(frame, "流通市值"),
        "reason_raw": frame["上榜原因"].astype(str),
    })
    return output.sort_values(
        ["trade_date", "code", "lhb_turnover"], kind="mergesort")


def normalize_institution(frame):
    _required(frame, INSTITUTION_REQUIRED, "lhb_institution")
    columns = [
        "trade_date", "code", "buyer_count", "seller_count",
        "institution_buy", "institution_sell", "institution_net_buy",
        "market_turnover", "turnover_pct", "free_float_market_cap",
        "reason_raw",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    output = pd.DataFrame({
        "trade_date": pd.to_datetime(frame["上榜日期"]).dt.strftime(
            "%Y%m%d").astype(int),
        "code": frame["代码"].astype(str).str.split(".").str[0].str.zfill(6),
        "buyer_count": _number(frame, "买方机构数"),
        "seller_count": _number(frame, "卖方机构数"),
        "institution_buy": _number(frame, "机构买入总额"),
        "institution_sell": _number(frame, "机构卖出总额"),
        "institution_net_buy": _number(frame, "机构买入净额"),
        "market_turnover": _number(frame, "市场总成交额"),
        "turnover_pct": _number(frame, "换手率"),
        "free_float_market_cap": _number(frame, "流通市值"),
        "reason_raw": frame["上榜原因"].astype(str),
    })
    return output.sort_values(
        ["trade_date", "code", "institution_buy"], kind="mergesort")


def _complete_manifest(month_root, expected_sessions):
    expected = set(int(value) for value in expected_sessions)
    candidates = []
    for path in sorted(Path(month_root).glob("*/manifest.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        covered = set(int(value) for value in payload.get(
            "covered_sessions", ()))
        if (payload.get("status") == "complete" and
                payload.get("dataset_version") == DATASET_VERSION and
                expected.issubset(covered)):
            candidates.append((path, payload))
    return candidates[-1] if candidates else None


def download_history(*, client, calendar_dates, output_dir, start_date,
                     end_date, attempts=4, sleep_seconds=.25,
                     sleep_fn=time.sleep, now_fn=None, progress=None):
    now_fn = now_fn or (lambda: datetime.now(SHANGHAI))
    output_dir = Path(output_dir)
    calendar = sorted({int(value) for value in calendar_dates
                       if int(start_date) <= int(value) <= int(end_date)})
    run = {
        "dataset_version": DATASET_VERSION,
        "started_at": now_fn().isoformat(),
        "start_date": int(start_date), "end_date": int(end_date),
        "availability_evidence": "BACKFILLED_QUERY",
        "strict_pit_allowed": False, "months": [],
    }
    for query_start, query_end in month_windows(start_date, end_date):
        expected = [value for value in calendar
                    if query_start <= value <= query_end]
        if not expected:
            continue
        month = str(query_start)[:6]
        month_root = output_dir / "batches" / month
        existing = _complete_manifest(month_root, expected)
        if existing is not None:
            item = {"month": month, "status": "skipped_complete",
                    "manifest": str(existing[0])}
            run["months"].append(item)
            if progress:
                progress(item)
            continue
        batch_id = "{}_{}".format(
            now_fn().strftime("%Y%m%dT%H%M%S%f%z"), uuid.uuid4().hex[:8])
        batch_dir = month_root / batch_id
        batch_dir.mkdir(parents=True, exist_ok=False)
        manifest = {
            "dataset_version": DATASET_VERSION, "batch_id": batch_id,
            "month": month, "query_start": query_start,
            "query_end": query_end, "captured_at": now_fn().isoformat(),
            "availability_evidence": "BACKFILLED_QUERY",
            "strict_pit_allowed": False, "covered_sessions": expected,
            "forbidden_detail_columns": list(FORBIDDEN_DETAIL_COLUMNS),
        }
        try:
            detail_raw = _retry(
                lambda: client.stock_lhb_detail_em(
                    start_date=str(query_start), end_date=str(query_end)),
                attempts, sleep_seconds, sleep_fn)
            sleep_fn(float(sleep_seconds))
            institution_raw = _retry(
                lambda: client.stock_lhb_jgmmtj_em(
                    start_date=str(query_start), end_date=str(query_end)),
                attempts, sleep_seconds, sleep_fn)
            detail = normalize_detail(detail_raw)
            institution = normalize_institution(institution_raw)
            raw_path = batch_dir / "provider_frames.json.gz"
            detail_path = batch_dir / "lhb_detail.csv.gz"
            institution_path = batch_dir / "lhb_institution.csv.gz"
            _atomic_gzip_json(raw_path, {
                "detail_columns": list(detail_raw.columns),
                "detail_records": detail_raw.replace(
                    {np.nan: None}).to_dict("records"),
                "institution_columns": list(institution_raw.columns),
                "institution_records": institution_raw.replace(
                    {np.nan: None}).to_dict("records"),
            })
            detail.to_csv(detail_path, index=False, compression={
                "method": "gzip", "compresslevel": 3, "mtime": 0})
            institution.to_csv(institution_path, index=False, compression={
                "method": "gzip", "compresslevel": 3, "mtime": 0})
            manifest.update({
                "status": "complete", "detail_rows": int(len(detail)),
                "institution_rows": int(len(institution)),
                "detail_event_sessions": sorted(set(
                    detail.trade_date.astype(int))) if len(detail) else [],
                "institution_event_sessions": sorted(set(
                    institution.trade_date.astype(int)))
                if len(institution) else [],
                "raw_path": str(raw_path), "raw_sha256": _sha256(raw_path),
                "detail_path": str(detail_path),
                "detail_sha256": _sha256(detail_path),
                "institution_path": str(institution_path),
                "institution_sha256": _sha256(institution_path),
            })
        except Exception as error:
            manifest.update({
                "status": "error", "covered_sessions": [],
                "error_code": "SOURCE_REQUEST_FAILED",
                "error_message": "{}: {}".format(
                    type(error).__name__, str(error)[:500]),
            })
        _atomic_bytes(batch_dir / "manifest.json", _json_bytes(manifest))
        item = {"month": month, "status": manifest["status"],
                "manifest": str(batch_dir / "manifest.json"),
                "detail_rows": int(manifest.get("detail_rows", 0)),
                "institution_rows": int(manifest.get("institution_rows", 0))}
        run["months"].append(item)
        if progress:
            progress(item)
        sleep_fn(float(sleep_seconds))
    counts = pd.Series([item["status"] for item in run["months"]]).value_counts()
    run["status_counts"] = {str(key): int(value)
                            for key, value in counts.items()}
    run["completed_at"] = now_fn().isoformat()
    run_name = run["started_at"].replace(":", "").replace("+", "_")
    _atomic_bytes(output_dir / "_runs" / (run_name + ".json"),
                  _json_bytes(run))
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=int, default=20200101)
    parser.add_argument("--end-date", type=int, default=20260930)
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research/akshare_lhb_history_v1"))
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--sleep-seconds", type=float, default=.25)
    args = parser.parse_args()
    import akshare as ak
    calendar = pd.to_datetime(
        ak.tool_trade_date_hist_sina()["trade_date"]).dt.strftime(
            "%Y%m%d").astype(int)
    result = download_history(
        client=ak, calendar_dates=calendar, output_dir=args.output_dir,
        start_date=args.start_date, end_date=args.end_date,
        attempts=args.attempts, sleep_seconds=args.sleep_seconds,
        progress=lambda item: print(json.dumps(
            item, ensure_ascii=False), flush=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    failed = sum(value for key, value in result["status_counts"].items()
                 if key not in ("complete", "skipped_complete"))
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
