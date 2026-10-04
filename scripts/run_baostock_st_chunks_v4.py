#!/usr/bin/env python3
"""Run frozen Baostock ST-exclusion chunks with bounded concurrency."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "scripts/collect_baostock_pit_pilot_v4.py"
DEFAULT_CONFIG = ROOT / "configs/selection/baostock_st_exclusion_v4.json"


def _completed(output_dir):
    report_path = output_dir / "collection_report.json"
    data_path = output_dir / "baostock_daily.jsonl"
    if not report_path.is_file() or not data_path.is_file():
        return False
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return report.get("status") == "COLLECTED_ST_EXCLUSION_CHUNK" and \
        not report.get("failures")


def run_chunks(universe_dir, collection_root, config_path, workers=3,
               chunk_ids=None):
    universe_dir = Path(universe_dir)
    collection_root = Path(collection_root)
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    maximum = int(config.get("max_concurrency", 1))
    if int(workers) > maximum:
        raise ValueError(
            "requested workers exceed source concurrency limit: {}".format(
                maximum))
    manifest = json.loads((universe_dir / "universe_manifest.json").read_text(
        encoding="utf-8"))
    requested = set(chunk_ids or [item["chunk_id"]
                                  for item in manifest["chunks"]])
    known = {item["chunk_id"] for item in manifest["chunks"]}
    unknown = requested - known
    if unknown:
        raise ValueError("unknown chunk ids: " + ",".join(sorted(unknown)))
    pending = []
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
        pending.append((chunk_id, universe_dir / "chunks" / chunk_id,
                        output_dir))

    def execute(item):
        chunk_id, input_dir, output_dir = item
        command = [
            sys.executable, str(COLLECTOR), str(input_dir),
            "--config", str(config_path),
            "--output-dir", str(output_dir),
        ]
        result = subprocess.run(
            command, cwd=str(ROOT), capture_output=True, text=True)
        return {
            "chunk_id": chunk_id,
            "returncode": result.returncode,
            "stdout_tail": result.stdout[-2000:],
            "stderr_tail": result.stderr[-2000:],
        }

    results = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, int(workers))) as executor:
        futures = {executor.submit(execute, item): item[0]
                   for item in pending}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps({
                "chunk_id": result["chunk_id"],
                "returncode": result["returncode"],
                "completed": result["returncode"] == 0,
            }, ensure_ascii=False), flush=True)
    results.sort(key=lambda item: item["chunk_id"])
    failed = [item for item in results if item["returncode"] != 0]
    return {
        "requested_chunk_count": len(requested),
        "skipped_completed_chunks": sorted(skipped),
        "executed_chunk_count": len(results),
        "completed_chunk_count": len(results) - len(failed),
        "failed_chunks": failed,
        "status": "COLLECTED_REQUESTED_ST_CHUNKS" if not failed else
        "BLOCKED_ST_CHUNK_COLLECTION",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("universe_dir", type=Path)
    parser.add_argument("collection_root", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--chunks", nargs="*")
    args = parser.parse_args()
    report = run_chunks(
        args.universe_dir, args.collection_root, args.config,
        workers=args.workers, chunk_ids=args.chunks)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not report["failed_chunks"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
