#!/usr/bin/env python3
"""Audit exact-date Baostock ST exclusion coverage against local price days."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/selection/baostock_st_exclusion_v4.json"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_rows(path):
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def build_audit(universe_dir, collection_roots, research_dir, config):
    universe_dir = Path(universe_dir)
    if isinstance(collection_roots, (str, Path)):
        collection_roots = [collection_roots]
    collection_roots = [Path(root) for root in collection_roots]
    research_dir = Path(research_dir)
    manifest_path = universe_dir / "universe_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if sha256_file(config["security_master"]) != \
            manifest["security_master_sha256"]:
        raise ValueError("security master changed after universe freeze")

    expected_days = 0
    covered_days = 0
    duplicate_rows = 0
    invalid_role_rows = 0
    derived_share_rows = 0
    st_counts = Counter()
    exchange_totals = defaultdict(lambda: Counter(expected=0, covered=0))
    unknown_by_symbol = Counter()
    completed_chunks = []
    missing_chunks = []
    failed_chunks = []

    for chunk in manifest["chunks"]:
        chunk_id = chunk["chunk_id"]
        input_path = universe_dir / chunk["input"]
        if sha256_file(input_path) != chunk["input_sha256"]:
            raise ValueError("frozen chunk input changed: " + chunk_id)
        frozen = json.loads(input_path.read_text(encoding="utf-8"))
        candidates = []
        failed_candidates = []
        for collection_root in collection_roots:
            output_dir = collection_root / "chunks" / chunk_id
            report_path = output_dir / "collection_report.json"
            data_path = output_dir / "baostock_daily.jsonl"
            if not report_path.is_file() or not data_path.is_file():
                continue
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("status") == "COLLECTED_ST_EXCLUSION_CHUNK" and \
                    not report.get("failures"):
                candidates.append((output_dir, data_path))
            else:
                failed_candidates.append(str(output_dir))
        if not candidates:
            if failed_candidates:
                failed_chunks.append({
                    "chunk_id": chunk_id, "outputs": failed_candidates})
            else:
                missing_chunks.append(chunk_id)
            continue
        output_dir, data_path = candidates[0]

        observed = defaultdict(set)
        for row in _read_rows(data_path):
            symbol = str(row["symbol"])
            day = int(row["date"])
            key = (symbol, day)
            if day in observed[symbol]:
                duplicate_rows += 1
            observed[symbol].add(day)
            if row.get("source_role") != "st_exclusion_only":
                invalid_role_rows += 1
            if row.get("derived_float_shares") is not None:
                derived_share_rows += 1
            st_counts[int(row["is_st"])] += 1

        for symbol in frozen["symbols"]:
            date_range = frozen["symbol_ranges"][symbol]
            start = int(date_range["start_date"].replace("-", ""))
            end = int(date_range["end_date"].replace("-", ""))
            raw_path = research_dir / "raw" / (symbol + ".csv")
            if not raw_path.is_file():
                continue
            frame = pd.read_csv(raw_path, usecols=["date"])
            days = pd.to_numeric(frame.date, errors="coerce").dropna().astype(int)
            days = set(days[(days >= start) & (days <= end)].tolist())
            present = days.intersection(observed.get(symbol, set()))
            expected_days += len(days)
            covered_days += len(present)
            exchange = symbol[:2]
            exchange_totals[exchange]["expected"] += len(days)
            exchange_totals[exchange]["covered"] += len(present)
            if len(present) < len(days):
                unknown_by_symbol[symbol] += len(days) - len(present)
        completed_chunks.append(chunk_id)

    coverage = covered_days / expected_days if expected_days else 0.0
    threshold = float(config["strict_rules"]["security_day_coverage_min"])
    full_collection = (
        len(completed_chunks) == manifest["chunk_count"] and
        not missing_chunks and not failed_chunks)
    clean = not duplicate_rows and not invalid_role_rows and \
        not derived_share_rows
    passed = full_collection and clean and coverage >= threshold
    return {
        "universe_manifest": str(manifest_path),
        "universe_manifest_sha256": sha256_file(manifest_path),
        "source_role": "st_exclusion_only",
        "collection_roots": [str(root) for root in collection_roots],
        "security_count": manifest["security_count"],
        "chunk_count": manifest["chunk_count"],
        "completed_chunk_count": len(completed_chunks),
        "completed_chunks": completed_chunks,
        "missing_chunks": missing_chunks,
        "failed_chunks": failed_chunks,
        "expected_local_price_security_days": expected_days,
        "exact_st_covered_security_days": covered_days,
        "exact_st_coverage": coverage,
        "coverage_threshold": threshold,
        "exchange_coverage": {
            exchange: {
                "expected": counts["expected"],
                "covered": counts["covered"],
                "coverage": (counts["covered"] / counts["expected"]
                             if counts["expected"] else 0.0),
            } for exchange, counts in sorted(exchange_totals.items())
        },
        "st_value_counts": dict(sorted(st_counts.items())),
        "duplicate_rows": duplicate_rows,
        "invalid_role_rows": invalid_role_rows,
        "derived_share_rows": derived_share_rows,
        "unknown_security_days": expected_days - covered_days,
        "top_unknown_symbols": [
            {"symbol": symbol, "unknown_days": count}
            for symbol, count in unknown_by_symbol.most_common(20)
        ],
        "unknown_st_policy": "exclude",
        "selection_semantics": (
            "is_st=1 excluded; missing exact-date state excluded; "
            "field forbidden as alpha/ranking input"),
        "gate_status": ("PASS_ST_EXCLUSION_DATA_GATE" if passed else
                        "BLOCKED_ST_EXCLUSION_DATA_GATE"),
        "research_label": config["research_label"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("universe_dir", type=Path)
    parser.add_argument("collection_roots", type=Path, nargs="+")
    parser.add_argument("--research-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite ST exclusion audit")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report = build_audit(
        args.universe_dir, args.collection_roots, args.research_dir, config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["gate_status"].startswith("PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
