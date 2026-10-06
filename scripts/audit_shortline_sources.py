#!/usr/bin/env python3
"""Read-only AKShare/eltdx coverage and availability probe."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuShortLineEvents import (  # noqa: E402
    ShortLineSnapshotMeta, availability_evidence, classify_probe_result,
    stable_payload_hash, stable_schema_hash,
)


AKSHARE_DATASETS = (
    "stock_zt_pool_em", "stock_zt_pool_dtgc_em", "stock_zt_pool_zbgc_em",
    "stock_zt_pool_previous_em", "stock_zt_pool_strong_em",
)

EVENT_STATUS_BY_AKSHARE_DATASET = {
    "stock_zt_pool_em": "limit_up",
    "stock_zt_pool_zbgc_em": "broken",
    "stock_zt_pool_dtgc_em": "limit_down",
}

ELTDX_LIMIT_FIELDS = (
    "trading_date_value", "code", "full_code", "name", "status",
    "board_level", "highest_board_level", "industry", "limit_reason",
    "limit_reason_extra", "seal_amount", "limit_time", "broken_count",
)


def probe_call_with_frame(call, *, source, dataset, query_date, ingested_at,
                          ingested_date):
    try:
        frame = call()
        return classify_probe_result(
            frame, source=source, dataset=dataset, query_date=query_date,
            ingested_at=ingested_at, ingested_date=ingested_date), frame
    except Exception as error:
        message = str(error)
        code = ("OUTSIDE_PROVIDER_RETENTION" if "30 个交易日" in message
                else "SOURCE_REQUEST_FAILED")
        return ShortLineSnapshotMeta(
            source=source, dataset=dataset, query_date=int(query_date),
            ingested_at=ingested_at,
            availability_evidence=availability_evidence(query_date, ingested_date),
            status="error", row_count=0,
            schema_sha256=stable_schema_hash([]),
            payload_sha256=stable_payload_hash([]),
            error_code=code, error_message="{}: {}".format(
                type(error).__name__, message[:240]),
        ), pd.DataFrame()


def probe_call(call, *, source, dataset, query_date, ingested_at, ingested_date):
    return probe_call_with_frame(
        call, source=source, dataset=dataset, query_date=query_date,
        ingested_at=ingested_at, ingested_date=ingested_date)[0]


def retry_call(call, attempts=3, delay_seconds=2.0):
    last = None
    for attempt in range(int(attempts)):
        try:
            return call()
        except Exception as error:
            last = error
            if attempt + 1 < int(attempts):
                time.sleep(float(delay_seconds) * (attempt + 1))
    raise last


def eltdx_limit_frame(result):
    """Convert eltdx model rows into a stable audit frame."""
    return pd.DataFrame([
        {field: getattr(row, field, None) for field in ELTDX_LIMIT_FIELDS}
        for row in result.rows
    ], columns=ELTDX_LIMIT_FIELDS)


def compare_event_sets(frames, dates):
    rows = []
    for query_date in dates:
        eltdx = frames.get(("eltdx_tdx", "limit_up_down_list", query_date))
        if eltdx is None or eltdx.empty:
            continue
        for dataset, status in EVENT_STATUS_BY_AKSHARE_DATASET.items():
            akshare = frames.get(("akshare_eastmoney", dataset, query_date))
            if akshare is None or akshare.empty or "代码" not in akshare:
                continue
            left = set(eltdx.loc[eltdx.status == status, "code"].astype(str))
            right = set(akshare["代码"].astype(str).str.zfill(6))
            union = left | right
            rows.append({
                "query_date": int(query_date), "event_status": status,
                "eltdx_count": len(left), "akshare_count": len(right),
                "intersection_count": len(left & right),
                "jaccard": float(len(left & right) / len(union)) if union else 1.0,
                "only_eltdx": sorted(left - right),
                "only_akshare": sorted(right - left),
            })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dates", nargs="+", type=int,
                        default=[20220104, 20241008, 20260930])
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research/shortline_source_audit"))
    parser.add_argument("--enable-eltdx", action="store_true")
    parser.add_argument("--eltdx-timeout", type=float, default=15.0)
    parser.add_argument("--eltdx-attempts", type=int, default=3)
    args = parser.parse_args()
    import akshare as ak

    now = datetime.now().astimezone()
    ingested_at = now.isoformat()
    ingested_date = int(now.strftime("%Y%m%d"))
    rows = []
    frames = {}
    for dataset in AKSHARE_DATASETS:
        function = getattr(ak, dataset)
        for query_date in args.dates:
            meta, frame = probe_call_with_frame(
                lambda function=function, value=query_date:
                function(date=str(value)),
                source="akshare_eastmoney", dataset=dataset,
                query_date=query_date, ingested_at=ingested_at,
                ingested_date=ingested_date)
            rows.append(meta.to_dict())
            if meta.status == "success":
                frames[("akshare_eastmoney", dataset, query_date)] = frame
    try:
        from eltdx import F10Client
        eltdx_version = importlib.metadata.version("eltdx")
        if args.enable_eltdx:
            client = F10Client(timeout=float(args.eltdx_timeout))
            for query_date in args.dates:
                meta, frame = probe_call_with_frame(
                    lambda value=query_date: eltdx_limit_frame(retry_call(
                        lambda: client.limit_up_down_list(str(value)),
                        attempts=args.eltdx_attempts)),
                    source="eltdx_tdx", dataset="limit_up_down_list",
                    query_date=query_date, ingested_at=ingested_at,
                    ingested_date=ingested_date)
                rows.append(meta.to_dict())
                if meta.status == "success":
                    frames[("eltdx_tdx", "limit_up_down_list", query_date)] = frame
            eltdx_status = "queried_read_only"
        else:
            eltdx_status = "installed_not_queried_enable_eltdx_required"
    except ImportError:
        eltdx_status = "unavailable_dependency_not_installed"
        eltdx_version = None
    comparisons = compare_event_sets(frames, args.dates)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    coverage = pd.DataFrame(rows)
    coverage.to_csv(args.output_dir / "source_coverage_matrix.csv", index=False)
    report = {
        "audit_version": "shortline_source_audit_v1",
        "ingested_at": ingested_at,
        "akshare_version": getattr(ak, "__version__", "unknown"),
        "eltdx_status": eltdx_status,
        "eltdx_version": eltdx_version,
        "results": rows,
        "cross_source_comparisons": comparisons,
        "policy": {
            "empty_response": "UNKNOWN_NOT_ZERO_EVENTS",
            "backfilled_query": "RESEARCH_OR_LABEL_ONLY_NOT_ASOF_FEATURE",
            "forward_capture": "ASOF_ALLOWED_ONLY_AFTER_SCHEMA_AND_COMPLETENESS_CHECK",
        },
    }
    (args.output_dir / "source_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "rows": len(rows), "status_counts": coverage.status.value_counts().to_dict(),
        "eltdx_status": eltdx_status,
        "cross_source_comparisons": comparisons,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
