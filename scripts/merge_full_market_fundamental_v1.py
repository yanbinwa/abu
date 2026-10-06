#!/usr/bin/env python3
"""Verify and merge immutable full-market structured-fundamental chunks."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.freeze_full_market_fundamental_universe_v1 import sha256_file


FACTS_FILE = "structured_fundamental_facts.jsonl"


def _rows(path):
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def merge(universe_dir, collection_roots, output_dir, config, mapping):
    universe_dir, output_dir = Path(universe_dir), Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite merged fundamentals")
    if isinstance(collection_roots, (str, Path)):
        collection_roots = [collection_roots]
    collection_roots = [Path(root) for root in collection_roots]
    manifest_path = universe_dir / "universe_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    completed, missing, failed, facts = [], [], [], []
    seen_keys, seen_by_statement = set(), defaultdict(set)
    field_symbols = defaultdict(set)
    duplicate_keys = out_of_chunk = incomplete_revisions = 0
    explicit_empty = []
    for chunk in manifest["chunks"]:
        chunk_id = chunk["chunk_id"]
        frozen_path = universe_dir / chunk["input"]
        if sha256_file(frozen_path) != chunk["input_sha256"]:
            raise ValueError("frozen fundamental chunk changed: " + chunk_id)
        frozen_symbols = set(json.loads(frozen_path.read_text(
            encoding="utf-8"))["symbols"])
        candidates, blocked = [], []
        for root in collection_roots:
            directory = root / "chunks" / chunk_id
            report_path = directory / "collection_report.json"
            facts_path = directory / FACTS_FILE
            if not report_path.is_file() or not facts_path.is_file():
                continue
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if (report.get("status") ==
                    "COLLECTED_FULL_MARKET_FUNDAMENTAL_CHUNK" and
                    not report.get("failures")):
                candidates.append((report, facts_path))
            else:
                blocked.append(str(directory))
        if not candidates:
            (failed if blocked else missing).append(
                {"chunk_id": chunk_id, "outputs": blocked}
                if blocked else chunk_id)
            continue
        report, facts_path = candidates[0]
        explicit_empty.extend(report.get("explicit_empty_statements", []))
        audit_pairs = {(item["symbol"], item["dataset"])
                       for item in report.get("dataset_audits", [])}
        expected_pairs = {(symbol, statement)
                          for symbol in frozen_symbols
                          for statement in config["required_statements"]}
        if audit_pairs != expected_pairs:
            failed.append({"chunk_id": chunk_id,
                           "reason": "statement_attempt_audit_mismatch"})
            continue
        for row in _rows(facts_path):
            symbol = str(row.get("symbol"))
            out_of_chunk += int(symbol not in frozen_symbols)
            key = str(row.get("source_key"))
            duplicate_keys += int(key in seen_keys)
            seen_keys.add(key)
            statement = str(row.get("statement_type"))
            seen_by_statement[statement].add(symbol)
            field_symbols[str(row.get("raw_field"))].add(symbol)
            incomplete_revisions += int(
                not row.get("revision_history_complete", False))
            facts.append(row)
        completed.append(chunk_id)
    facts.sort(key=lambda item: (
        item["symbol"], item["statement_type"], item["report_period"],
        item["source_key"]))
    output_dir.mkdir(parents=True)
    facts_path = output_dir / FACTS_FILE
    facts_path.write_text("".join(json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) +
        "\n" for row in facts), encoding="utf-8")
    count = int(manifest["security_count"])
    statement_coverage = {
        statement: len(seen_by_statement[statement]) / count if count else 0
        for statement in config["required_statements"]
    }
    field_coverage = {
        field: len(field_symbols[field]) / count if count else 0
        for field in mapping["fields"]
    }
    collection_passed = (
        len(completed) == manifest["chunk_count"] and not missing and
        not failed and not duplicate_keys and not out_of_chunk)
    thresholds = config["strict_rules"]
    coverage_passed = (
        all(value >= thresholds["statement_symbol_coverage_min"]
            for value in statement_coverage.values()) and
        all(value >= thresholds["required_field_symbol_coverage_min"]
            for value in field_coverage.values()))
    report = {
        "universe_manifest": str(manifest_path),
        "universe_manifest_sha256": sha256_file(manifest_path),
        "collection_roots": [str(root) for root in collection_roots],
        "security_count": count, "chunk_count": manifest["chunk_count"],
        "completed_chunk_count": len(completed),
        "missing_chunks": missing, "failed_chunks": failed,
        "fact_value_count": len(facts),
        "explicit_empty_statements": explicit_empty,
        "duplicate_source_keys": duplicate_keys,
        "out_of_chunk_rows": out_of_chunk,
        "statement_symbol_coverage": statement_coverage,
        "required_field_symbol_coverage": field_coverage,
        "incomplete_revision_rows": incomplete_revisions,
        "collection_gate_status": (
            "PASS_FULL_MARKET_FUNDAMENTAL_COLLECTION_GATE"
            if collection_passed else
            "BLOCKED_FULL_MARKET_FUNDAMENTAL_COLLECTION_GATE"),
        "coverage_gate_status": (
            "PASS_FULL_MARKET_FUNDAMENTAL_COVERAGE_GATE"
            if coverage_passed else
            "BLOCKED_FULL_MARKET_FUNDAMENTAL_COVERAGE_GATE"),
        "revision_gate_status": "BLOCKED_HISTORICAL_REVISION_VERSIONS",
        "outputs": {"facts": str(facts_path),
                    "facts_sha256": sha256_file(facts_path)},
        "research_label": config["research_label"],
    }
    report_path = output_dir / "merge_report.json"
    report_path.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return report, report_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("universe_dir", type=Path)
    parser.add_argument("collection_roots", type=Path, nargs="+")
    parser.add_argument("--config", type=Path, default=(
        ROOT / "configs/selection/fundamental_full_market_v1.json"))
    parser.add_argument("--mapping", type=Path, default=(
        ROOT / "configs/selection/fundamental_structured_field_mapping_v4.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    report, path = merge(args.universe_dir, args.collection_roots,
                         args.output_dir, config, mapping)
    print(json.dumps({**report, "report": str(path)}, ensure_ascii=False,
                     indent=2, sort_keys=True))
    return 0 if report["collection_gate_status"].startswith("PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
