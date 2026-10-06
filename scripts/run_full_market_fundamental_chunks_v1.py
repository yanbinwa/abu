#!/usr/bin/env python3
"""Run frozen full-market fundamental chunks sequentially and resumably."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "scripts/collect_full_market_fundamental_chunk_v1.py"
DEFAULT_CONFIG = ROOT / "configs/selection/fundamental_full_market_v1.json"
DEFAULT_MAPPING = ROOT / "configs/selection/fundamental_structured_field_mapping_v4.json"


def _config_sha256(config):
    return hashlib.sha256(json.dumps(
        config, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _completed(output_dir, expected_symbols, expected_config_sha256):
    report_path = output_dir / "collection_report.json"
    facts_path = output_dir / "structured_fundamental_facts.jsonl"
    if not report_path.is_file() or not facts_path.is_file():
        return False
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return (report.get("status") ==
            "COLLECTED_FULL_MARKET_FUNDAMENTAL_CHUNK" and
            not report.get("failures") and
            int(report.get("symbol_count", -1)) == int(expected_symbols) and
            report.get("config_sha256") == expected_config_sha256)


def run_chunks(universe_dir, collection_root, config_path=DEFAULT_CONFIG,
               mapping_path=DEFAULT_MAPPING, workers=1, chunk_ids=None):
    universe_dir, collection_root = Path(universe_dir), Path(collection_root)
    config_path, mapping_path = Path(config_path), Path(mapping_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config_sha256 = _config_sha256(config)
    if int(workers) != 1 or int(workers) > int(config["max_concurrency"]):
        raise ValueError("fundamental collection must run sequentially")
    manifest = json.loads((universe_dir / "universe_manifest.json").read_text(
        encoding="utf-8"))
    requested = set(chunk_ids or [item["chunk_id"]
                                  for item in manifest["chunks"]])
    known = {item["chunk_id"] for item in manifest["chunks"]}
    if requested - known:
        raise ValueError("unknown chunk ids: " +
                         ",".join(sorted(requested - known)))
    results, skipped = [], []
    for chunk in manifest["chunks"]:
        chunk_id = chunk["chunk_id"]
        if chunk_id not in requested:
            continue
        output_dir = collection_root / "chunks" / chunk_id
        if _completed(output_dir, chunk["symbol_count"], config_sha256):
            skipped.append(chunk_id)
            continue
        if output_dir.exists():
            raise FileExistsError(
                "incomplete chunk requires a new collection root: " +
                str(output_dir))
        command = [
            sys.executable, str(COLLECTOR),
            str(universe_dir / "chunks" / chunk_id),
            "--config", str(config_path), "--mapping", str(mapping_path),
            "--output-dir", str(output_dir),
        ]
        process = subprocess.run(
            command, cwd=str(ROOT), capture_output=True, text=True)
        result = {
            "chunk_id": chunk_id, "returncode": process.returncode,
            "stdout_tail": process.stdout[-2000:],
            "stderr_tail": process.stderr[-2000:],
        }
        results.append(result)
        print(json.dumps({
            "chunk_id": chunk_id, "returncode": process.returncode,
            "completed": process.returncode == 0,
        }, ensure_ascii=False), flush=True)
        if process.returncode != 0:
            break
    failures = [item for item in results if item["returncode"] != 0]
    return {
        "requested_chunk_count": len(requested),
        "skipped_completed_chunks": sorted(skipped),
        "executed_chunk_count": len(results),
        "completed_chunk_count": len(results) - len(failures),
        "failed_chunks": failures,
        "status": ("COLLECTED_REQUESTED_FULL_MARKET_FUNDAMENTAL_CHUNKS"
                   if not failures else
                   "BLOCKED_FULL_MARKET_FUNDAMENTAL_COLLECTION"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("universe_dir", type=Path)
    parser.add_argument("collection_root", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--chunks", nargs="*")
    args = parser.parse_args()
    report = run_chunks(
        args.universe_dir, args.collection_root, args.config, args.mapping,
        args.workers, args.chunks)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not report["failed_chunks"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
