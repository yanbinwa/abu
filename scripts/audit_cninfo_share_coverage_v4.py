#!/usr/bin/env python3
"""Audit full-market CNINFO share-capital collection and fact coverage."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/selection/cninfo_share_capital_full_v4.json"


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


def build_audit(universe_dir, collection_roots, config):
    universe_dir = Path(universe_dir)
    if isinstance(collection_roots, (str, Path)):
        collection_roots = [collection_roots]
    collection_roots = [Path(root) for root in collection_roots]
    manifest_path = universe_dir / "universe_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if sha256_file(config["security_master"]) != \
            manifest["security_master_sha256"]:
        raise ValueError("security master changed after universe freeze")

    completed_chunks = []
    missing_chunks = []
    failed_chunks = []
    event_symbols = set()
    empty_symbols = set()
    event_count = 0
    duplicate_source_keys = 0
    invalid_rows = Counter()
    seen_keys = set()

    for chunk in manifest["chunks"]:
        chunk_id = chunk["chunk_id"]
        input_path = universe_dir / chunk["input"]
        if sha256_file(input_path) != chunk["input_sha256"]:
            raise ValueError("frozen chunk input changed: " + chunk_id)
        frozen = json.loads(input_path.read_text(encoding="utf-8"))
        frozen_symbols = set(frozen["symbols"])
        candidates = []
        failed_candidates = []
        for collection_root in collection_roots:
            output_dir = collection_root / "chunks" / chunk_id
            report_path = output_dir / "collection_report.json"
            data_path = output_dir / "share_events.jsonl"
            if not report_path.is_file() or not data_path.is_file():
                continue
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("status") == "COLLECTED_CNINFO_SHARE_CHUNK" and \
                    not report.get("failures"):
                candidates.append((report, data_path))
            else:
                failed_candidates.append(str(output_dir))
        if not candidates:
            if failed_candidates:
                failed_chunks.append({
                    "chunk_id": chunk_id, "outputs": failed_candidates})
            else:
                missing_chunks.append(chunk_id)
            continue
        report, data_path = candidates[0]
        report_empty = set(report.get("empty_symbols", []))
        if not report_empty.issubset(frozen_symbols):
            invalid_rows["REPORT_EMPTY_SYMBOL_OUTSIDE_CHUNK"] += 1
        empty_symbols.update(report_empty)
        for row in _read_rows(data_path):
            event_count += 1
            symbol = str(row.get("symbol"))
            source_key = str(row.get("source_key"))
            if symbol not in frozen_symbols:
                invalid_rows["EVENT_SYMBOL_OUTSIDE_CHUNK"] += 1
            if source_key in seen_keys:
                duplicate_source_keys += 1
            seen_keys.add(source_key)
            for field in ("total_shares", "float_shares"):
                value = row.get(field)
                if not isinstance(value, (int, float)) or value <= 0:
                    invalid_rows["INVALID_" + field.upper()] += 1
                elif float(value) != float(round(value)):
                    invalid_rows["FRACTIONAL_" + field.upper()] += 1
            if isinstance(row.get("total_shares"), (int, float)) and \
                    isinstance(row.get("float_shares"), (int, float)) and \
                    row["float_shares"] > row["total_shares"]:
                invalid_rows["FLOAT_ABOVE_TOTAL"] += 1
            event_symbols.add(symbol)
        completed_chunks.append(chunk_id)

    symbol_count = int(manifest["security_count"])
    coverage = len(event_symbols) / symbol_count if symbol_count else 0.0
    threshold = float(config["strict_rules"]["minimum_symbols_with_events"])
    collection_passed = (
        len(completed_chunks) == manifest["chunk_count"] and
        not missing_chunks and not failed_chunks and not invalid_rows and
        not duplicate_source_keys)
    coverage_passed = collection_passed and coverage >= threshold
    return {
        "universe_manifest": str(manifest_path),
        "universe_manifest_sha256": sha256_file(manifest_path),
        "collection_roots": [str(root) for root in collection_roots],
        "source_role": config["source_role"],
        "security_count": symbol_count,
        "chunk_count": manifest["chunk_count"],
        "completed_chunk_count": len(completed_chunks),
        "completed_chunks": completed_chunks,
        "missing_chunks": missing_chunks,
        "failed_chunks": failed_chunks,
        "share_event_count": event_count,
        "symbols_with_events": len(event_symbols),
        "symbols_with_events_coverage": coverage,
        "symbols_with_events_threshold": threshold,
        "explicit_empty_symbol_count": len(empty_symbols),
        "explicit_empty_symbols": sorted(empty_symbols),
        "duplicate_source_keys": duplicate_source_keys,
        "invalid_rows": dict(sorted(invalid_rows.items())),
        "collection_gate_status": (
            "PASS_CNINFO_SHARE_COLLECTION_GATE" if collection_passed else
            "BLOCKED_CNINFO_SHARE_COLLECTION_GATE"),
        "primary_fact_coverage_gate_status": (
            "PASS_CNINFO_PRIMARY_SHARE_COVERAGE" if coverage_passed else
            "BLOCKED_CNINFO_PRIMARY_SHARE_COVERAGE"),
        "empty_response_semantics": (
            "explicit missing fact; never zero; requires an audited fallback"),
        "research_label": config["research_label"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("universe_dir", type=Path)
    parser.add_argument("collection_roots", type=Path, nargs="+")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite CNINFO share audit")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report = build_audit(args.universe_dir, args.collection_roots, config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["primary_fact_coverage_gate_status"].startswith(
        "PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
