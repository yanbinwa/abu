#!/usr/bin/env python3
"""Run frozen structured balance fallback chunks sequentially."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "scripts/collect_balance_fallback_chunk_v4.py"
DEFAULT_CONFIG = ROOT / "configs/selection/fundamental_balance_fallback_v4.json"
DEFAULT_MAPPING = (
    ROOT / "configs/selection/fundamental_structured_field_mapping_v4.json"
)


def _completed(output_dir):
    report_path = output_dir / "collection_report.json"
    facts_path = output_dir / "structured_balance_facts.jsonl"
    if not report_path.is_file() or not facts_path.is_file():
        return False
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return report.get("status") == "COLLECTED_BALANCE_FALLBACK_CHUNK" and \
        not report.get("failures")


def run_chunks(universe_dir, collection_root, config_path, mapping_path,
               workers=1, chunk_ids=None):
    universe_dir = Path(universe_dir)
    collection_root = Path(collection_root)
    config_path = Path(config_path)
    mapping_path = Path(mapping_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if int(workers) != 1 or int(workers) > int(config["max_concurrency"]):
        raise ValueError("balance fallback collection must run sequentially")
    manifest = json.loads((universe_dir / "universe_manifest.json").read_text(
        encoding="utf-8"))
    requested = set(chunk_ids or [item["chunk_id"]
                                  for item in manifest["chunks"]])
    known = {item["chunk_id"] for item in manifest["chunks"]}
    unknown = requested - known
    if unknown:
        raise ValueError("unknown chunk ids: " + ",".join(sorted(unknown)))
    results = []
    skipped = []
    for chunk in manifest["chunks"]:
        chunk_id = chunk["chunk_id"]
        if chunk_id not in requested:
            continue
        output_dir = collection_root / "chunks" / chunk_id
        if _completed(output_dir):
            skipped.append(chunk_id)
            continue
        if output_dir.exists():
            raise FileExistsError(
                "incomplete chunk output requires a new collection root: " +
                str(output_dir))
        command = [
            sys.executable, str(COLLECTOR),
            str(universe_dir / "chunks" / chunk_id),
            "--config", str(config_path), "--mapping", str(mapping_path),
            "--output-dir", str(output_dir),
        ]
        result = subprocess.run(
            command, cwd=str(ROOT), capture_output=True, text=True)
        item = {
            "chunk_id": chunk_id, "returncode": result.returncode,
            "stdout_tail": result.stdout[-2000:],
            "stderr_tail": result.stderr[-2000:],
        }
        results.append(item)
        print(json.dumps({
            "chunk_id": chunk_id, "returncode": result.returncode,
            "completed": result.returncode == 0,
        }, ensure_ascii=False), flush=True)
        if result.returncode != 0:
            break
    failed = [item for item in results if item["returncode"] != 0]
    return {
        "requested_chunk_count": len(requested),
        "skipped_completed_chunks": sorted(skipped),
        "executed_chunk_count": len(results),
        "completed_chunk_count": len(results) - len(failed),
        "failed_chunks": failed,
        "status": "COLLECTED_REQUESTED_BALANCE_FALLBACK_CHUNKS"
        if not failed else "BLOCKED_BALANCE_FALLBACK_COLLECTION",
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
        workers=args.workers, chunk_ids=args.chunks)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not report["failed_chunks"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
