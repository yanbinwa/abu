#!/usr/bin/env python3
"""Read-only AKShare/eltdx coverage and availability probe."""
from __future__ import annotations

import argparse
import json
import sys
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


def probe_call(call, *, source, dataset, query_date, ingested_at, ingested_date):
    try:
        frame = call()
        return classify_probe_result(
            frame, source=source, dataset=dataset, query_date=query_date,
            ingested_at=ingested_at, ingested_date=ingested_date)
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
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dates", nargs="+", type=int,
                        default=[20220104, 20241008, 20260930])
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research/shortline_source_audit"))
    args = parser.parse_args()
    import akshare as ak

    now = datetime.now().astimezone()
    ingested_at = now.isoformat()
    ingested_date = int(now.strftime("%Y%m%d"))
    rows = []
    for dataset in AKSHARE_DATASETS:
        function = getattr(ak, dataset)
        for query_date in args.dates:
            meta = probe_call(
                lambda function=function, value=query_date:
                function(date=str(value)),
                source="akshare_eastmoney", dataset=dataset,
                query_date=query_date, ingested_at=ingested_at,
                ingested_date=ingested_date)
            rows.append(meta.to_dict())
    try:
        import eltdx  # noqa: F401
        eltdx_status = "installed_not_queried_without_endpoint_config"
    except ImportError:
        eltdx_status = "unavailable_dependency_not_installed"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    coverage = pd.DataFrame(rows)
    coverage.to_csv(args.output_dir / "source_coverage_matrix.csv", index=False)
    report = {
        "audit_version": "shortline_source_audit_v1",
        "ingested_at": ingested_at,
        "akshare_version": getattr(ak, "__version__", "unknown"),
        "eltdx_status": eltdx_status,
        "results": rows,
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
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

