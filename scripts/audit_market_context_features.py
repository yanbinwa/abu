#!/usr/bin/env python3
"""Audit M0A market, industry and trend-leader output tables."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _ordered_unique_chunks(path, keys, chunksize=200000):
    rows = 0
    duplicates = 0
    ordered = True
    last_key = None
    coverage_sum = 0.0
    coverage_count = 0
    coverage_min = np.inf
    coverage_max = -np.inf
    dates = set()
    symbols = set()
    hashes = set()
    for chunk in pd.read_csv(path, chunksize=chunksize):
        rows += len(chunk)
        duplicates += int(chunk.duplicated(keys).sum())
        tuples = list(chunk[keys].itertuples(index=False, name=None))
        if tuples:
            if last_key is not None and tuples[0] <= last_key:
                ordered = False
            if any(left >= right for left, right in zip(tuples, tuples[1:])):
                ordered = False
            last_key = tuples[-1]
        valid = pd.to_numeric(chunk.coverage_ratio, errors="coerce").dropna().to_numpy()
        if len(valid):
            coverage_sum += float(valid.sum())
            coverage_count += len(valid)
            coverage_min = min(coverage_min, float(valid.min()))
            coverage_max = max(coverage_max, float(valid.max()))
        dates.update(chunk.trade_date.unique().tolist())
        if "symbol" in chunk:
            symbols.update(chunk.symbol.unique().tolist())
        if "config_sha256" in chunk:
            hashes.update(chunk.config_sha256.unique().tolist())
    return {
        "rows": rows,
        "duplicate_keys": duplicates,
        "strictly_ordered": ordered,
        "dates": len(dates),
        "symbols": len(symbols),
        "coverage_min": coverage_min if coverage_count else None,
        "coverage_mean": coverage_sum / coverage_count if coverage_count else None,
        "coverage_max": coverage_max if coverage_count else None,
        "config_hashes": sorted(hashes),
    }


def audit_outputs(directory):
    directory = Path(directory)
    breadth = pd.read_csv(directory / "market_breadth_asof_close.csv.gz")
    industry = pd.read_csv(directory / "industry_strength_daily.csv.gz")
    report = {
        "breadth": {
            "rows": len(breadth),
            "dates": int(breadth.trade_date.nunique()),
            "duplicate_keys": int(breadth.duplicated(
                ["trade_date", "universe_scope"]).sum()),
            "scopes": breadth.groupby("universe_scope").size().to_dict(),
            "coverage_min": breadth.groupby("universe_scope").coverage_ratio.min().to_dict(),
            "coverage_mean": breadth.groupby("universe_scope").coverage_ratio.mean().to_dict(),
            "config_hashes": sorted(breadth.config_sha256.unique().tolist()),
        },
        "industry": {
            "rows": len(industry),
            "dates": int(industry.trade_date.nunique()),
            "industries": int(industry.industry_id.nunique()),
            "duplicate_keys": int(industry.duplicated(
                ["trade_date", "industry_id"]).sum()),
            "coverage_min": float(industry.coverage_ratio.min()),
            "coverage_mean": float(industry.coverage_ratio.mean()),
            "return_20d_nonnull": float(industry.return_20d.notna().mean()),
            "config_hashes": sorted(industry.config_sha256.unique().tolist()),
        },
        "trend_leader": _ordered_unique_chunks(
            directory / "industry_trend_leader_daily.csv.gz",
            ["trade_date", "industry_id", "symbol"]),
    }
    hashes = set(report["breadth"]["config_hashes"])
    hashes.update(report["industry"]["config_hashes"])
    hashes.update(report["trend_leader"]["config_hashes"])
    report["checks"] = {
        "unique_config_hash": len(hashes) == 1,
        "no_duplicate_keys": all(
            report[name]["duplicate_keys"] == 0
            for name in ("breadth", "industry", "trend_leader")),
        "trend_leader_strictly_ordered": report["trend_leader"]["strictly_ordered"],
        "breadth_two_complete_scopes": (
            len(report["breadth"]["scopes"]) == 2 and
            len(set(report["breadth"]["scopes"].values())) == 1),
    }
    report["passed"] = all(report["checks"].values())
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research/shortline_features/m0a"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_outputs(args.directory)
    output = args.output or args.directory / "m0a_quality_report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
