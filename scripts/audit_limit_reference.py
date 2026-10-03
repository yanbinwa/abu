#!/usr/bin/env python3
"""Audit whether local A-share datasets contain authoritative limit references."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import pandas as pd


REFERENCE_COLUMNS = {
    "pre_close", "previous_close", "reference_price",
    "limit_reference_price_raw",
}
DIRECT_LIMIT_COLUMNS = {
    "upper_limit", "lower_limit", "exchange_upper_limit_raw",
    "exchange_lower_limit_raw",
}


def _header(path):
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return next(csv.reader(handle), [])
    except (OSError, StopIteration, UnicodeDecodeError):
        return []


def audit_directory(name, directory, pattern, *, price_space, provenance,
                    pre_close_semantics):
    paths = sorted(Path(directory).glob(pattern))
    schemas = {}
    with_reference = 0
    with_direct_limits = 0
    for path in paths:
        columns = tuple(_header(path))
        schemas[columns] = schemas.get(columns, 0) + 1
        column_set = set(columns)
        with_reference += int(bool(column_set & REFERENCE_COLUMNS))
        with_direct_limits += int(
            {"upper_limit", "lower_limit"}.issubset(column_set) or
            {"exchange_upper_limit_raw", "exchange_lower_limit_raw"}.issubset(column_set)
        )
    return {
        "source": name,
        "directory": str(directory),
        "file_count": len(paths),
        "files_with_reference_column": with_reference,
        "files_with_direct_limit_columns": with_direct_limits,
        "schema_count": len(schemas),
        "price_space": price_space,
        "provenance": provenance,
        "pre_close_semantics": pre_close_semantics,
        "usable_for_limit_facts": bool(
            with_reference and pre_close_semantics == "provider_explicit_raw"
        ) or bool(with_direct_limits),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--research-dir", type=Path,
        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument(
        "--market-cache", type=Path,
        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("/Users/wjy/abu/data/selection_research/limit_reference"))
    args = parser.parse_args()

    rows = [
        audit_directory(
            "selection_research_raw", args.research_dir / "raw", "*.csv",
            price_space="raw", provenance="AKShare Sina/Tencent historical",
            pre_close_semantics="absent"),
        audit_directory(
            "abu_market_cache", args.market_cache, "*",
            price_space="qfq", provenance="AKShare adapter normalized cache",
            pre_close_semantics="adapter_shifted_adjusted_close"),
    ]
    # Provider schema capabilities are kept separate from local coverage.  A
    # future online collection run must add availability evidence before a row
    # may be normalized into the sidecar.
    rows.extend([
        {
            "source": "akshare_eastmoney_historical_candidate",
            "directory": "remote", "file_count": 0,
            "files_with_reference_column": 0,
            "files_with_direct_limit_columns": 0, "schema_count": 1,
            "price_space": "raw when adjust is empty",
            "provenance": "stock_zh_a_hist",
            "pre_close_semantics": "not_explicit; change/pct fields require validation",
            "usable_for_limit_facts": False,
        },
        {
            "source": "akshare_spot_snapshot_candidate",
            "directory": "remote", "file_count": 0,
            "files_with_reference_column": 0,
            "files_with_direct_limit_columns": 0, "schema_count": 1,
            "price_space": "raw", "provenance": "current spot snapshot",
            "pre_close_semantics": "provider_explicit_raw_but_not_historical",
            "usable_for_limit_facts": False,
        },
    ])

    args.output_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    frame.to_csv(args.output_dir / "limit_reference_source_coverage.csv", index=False)
    payload = {
        "schema_version": "limit_reference_source_audit_v1",
        "rows": rows,
        "conclusion": (
            "No current local dataset provides a validated raw historical "
            "pre_close/reference price or direct daily limits."
        ),
    }
    (args.output_dir / "limit_reference_source_audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

