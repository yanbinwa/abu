#!/usr/bin/env python3
"""Verify and merge frozen balance-sheet fallback fact chunks."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rows(path):
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def merge(universe_dir, collection_roots, output_dir):
    universe_dir = Path(universe_dir)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite merged balance facts")
    if isinstance(collection_roots, (str, Path)):
        collection_roots = [collection_roots]
    collection_roots = [Path(root) for root in collection_roots]
    manifest_path = universe_dir / "universe_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    facts = []
    completed = []
    missing_chunks = []
    failed_chunks = []
    empty_symbols = set()
    seen_symbols = set()
    seen_keys = set()
    duplicate_keys = 0
    out_of_chunk_rows = 0
    total_share_symbols = set()
    incomplete_revision_rows = 0
    for chunk in manifest["chunks"]:
        chunk_id = chunk["chunk_id"]
        frozen_path = universe_dir / chunk["input"]
        if sha256_file(frozen_path) != chunk["input_sha256"]:
            raise ValueError("frozen fallback chunk changed: " + chunk_id)
        frozen_symbols = set(json.loads(frozen_path.read_text(
            encoding="utf-8"))["symbols"])
        candidates = []
        failed = []
        for root in collection_roots:
            directory = root / "chunks" / chunk_id
            report_path = directory / "collection_report.json"
            facts_path = directory / "structured_balance_facts.jsonl"
            if not report_path.is_file() or not facts_path.is_file():
                continue
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("status") == "COLLECTED_BALANCE_FALLBACK_CHUNK" and \
                    not report.get("failures"):
                candidates.append((report, facts_path))
            else:
                failed.append(str(directory))
        if not candidates:
            if failed:
                failed_chunks.append({"chunk_id": chunk_id, "outputs": failed})
            else:
                missing_chunks.append(chunk_id)
            continue
        report, facts_path = candidates[0]
        empty_symbols.update(report.get("empty_symbols", []))
        for row in _rows(facts_path):
            symbol = str(row.get("symbol"))
            if symbol not in frozen_symbols:
                out_of_chunk_rows += 1
            seen_symbols.add(symbol)
            key = str(row.get("source_key"))
            duplicate_keys += int(key in seen_keys)
            seen_keys.add(key)
            if row.get("raw_field") == "SHARE_CAPITAL":
                total_share_symbols.add(symbol)
            incomplete_revision_rows += int(
                not row.get("revision_history_complete", False))
            facts.append(row)
        completed.append(chunk_id)
    facts.sort(key=lambda item: (
        item["symbol"], item["report_period"], item["source_key"]))
    collection_passed = (
        len(completed) == manifest["chunk_count"] and not missing_chunks and
        not failed_chunks and not duplicate_keys and not out_of_chunk_rows)
    output_dir.mkdir(parents=True)
    facts_path = output_dir / "structured_balance_facts.jsonl"
    share_path = output_dir / "structured_total_share_facts.jsonl"
    facts_path.write_text("".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")) + "\n" for row in facts),
        encoding="utf-8")
    share_path.write_text("".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")) + "\n" for row in facts
        if row.get("raw_field") == "SHARE_CAPITAL"), encoding="utf-8")
    symbol_count = int(manifest["fallback_symbol_count"])
    report = {
        "universe_manifest": str(manifest_path),
        "universe_manifest_sha256": sha256_file(manifest_path),
        "collection_roots": [str(root) for root in collection_roots],
        "chunk_count": manifest["chunk_count"],
        "completed_chunk_count": len(completed),
        "missing_chunks": missing_chunks, "failed_chunks": failed_chunks,
        "fallback_symbol_count": symbol_count,
        "symbols_with_any_facts": len(seen_symbols),
        "symbols_with_total_share_facts": len(total_share_symbols),
        "total_share_symbol_coverage": (
            len(total_share_symbols) / symbol_count if symbol_count else 0),
        "explicit_empty_symbol_count": len(empty_symbols),
        "explicit_empty_symbols": sorted(empty_symbols),
        "fact_value_count": len(facts),
        "total_share_fact_count": sum(
            row.get("raw_field") == "SHARE_CAPITAL" for row in facts),
        "duplicate_source_keys": duplicate_keys,
        "out_of_chunk_rows": out_of_chunk_rows,
        "incomplete_revision_rows": incomplete_revision_rows,
        "collection_gate_status": (
            "PASS_BALANCE_FALLBACK_COLLECTION_GATE" if collection_passed else
            "BLOCKED_BALANCE_FALLBACK_COLLECTION_GATE"),
        "revision_gate_status": "BLOCKED_HISTORICAL_REVISION_VERSIONS",
        "outputs": {
            "facts": str(facts_path), "facts_sha256": sha256_file(facts_path),
            "total_share_facts": str(share_path),
            "total_share_facts_sha256": sha256_file(share_path),
        },
        "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
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
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report, report_path = merge(
        args.universe_dir, args.collection_roots, args.output_dir)
    print(json.dumps({**report, "report": str(report_path)},
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["collection_gate_status"].startswith("PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
