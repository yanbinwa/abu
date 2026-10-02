#!/usr/bin/env python3
"""Freeze selection-research inputs and legacy outputs with stable hashes."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC_VERSION = "0.1.0"
SNAPSHOT_SCHEMA_VERSION = "1.0.0"
EXCLUDED_NAMES = {
    "snapshot_manifest.json",
    "baseline_registry.json",
    "coverage_daily.csv",
    "coverage_symbol.csv",
    "coverage_summary.json",
    "provider_provenance.csv",
}


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(root), text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _package_version(name: str) -> str | None:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return None


def _iter_files(namespace: str, directory: Path):
    if not directory.exists():
        return
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        if path.name in EXCLUDED_NAMES or "__pycache__" in path.parts:
            continue
        yield namespace, directory, path


def build_file_inventory(roots: list[tuple[str, Path]]) -> list[dict]:
    inventory = []
    for namespace, directory in sorted(roots, key=lambda item: item[0]):
        for _, root, path in _iter_files(namespace, directory):
            inventory.append(
                {
                    "path": f"{namespace}/{path.relative_to(root).as_posix()}",
                    "sha256": _sha256_file(path),
                    "bytes": path.stat().st_size,
                }
            )
    inventory.sort(key=lambda item: item["path"])
    return inventory


def stable_snapshot_id(files: list[dict], spec_version: str = SPEC_VERSION) -> str:
    payload = {"spec_version": spec_version, "files": files}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def build_snapshot_manifest(
    signal_dir: Path,
    research_dir: Path,
    code_root: Path = ROOT,
    created_at: str | None = None,
) -> dict:
    files = build_file_inventory(
        [("signal", signal_dir.resolve()), ("research", research_dir.resolve())]
    )
    research_manifest = _read_json(research_dir / "manifest.json")
    coverage_summary = _read_json(research_dir / "coverage_summary.json")
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "spec_version": SPEC_VERSION,
        "snapshot_id": stable_snapshot_id(files),
        "created_at": created_at or datetime.now().astimezone().isoformat(),
        "start_date": research_manifest.get("start"),
        "end_date": research_manifest.get("end"),
        "code_commit": _git_commit(code_root),
        "environment": {
            "python": platform.python_version(),
            "pandas": _package_version("pandas"),
            "numpy": _package_version("numpy"),
            "akshare": _package_version("akshare"),
        },
        "roots": {
            "signal": str(signal_dir.resolve()),
            "research": str(research_dir.resolve()),
        },
        "provider_coverage": research_manifest.get("price_providers", {}),
        "field_coverage": coverage_summary.get("field_coverage", {}),
        "known_limitations": research_manifest.get("limitations", []),
        "files": files,
    }


def build_baseline_registry(baseline_dirs: list[Path], code_commit: str) -> dict:
    roots = [(f"baseline_{position:02d}", path.resolve())
             for position, path in enumerate(baseline_dirs)]
    files = build_file_inventory(roots)
    return {
        "schema_version": "1.0.0",
        "created_at": datetime.now().astimezone().isoformat(),
        "code_commit": code_commit,
        "placebo_v1_status": "invalid_for_inference",
        "placebo_v1_reason": (
            "Candidate selection reads future exit-day tradability and uses a "
            "simplified adjusted-price path with copied strategy costs."
        ),
        "roots": [str(path.resolve()) for path in baseline_dirs],
        "files": files,
    }


def write_new_json(path: Path, payload: dict, force: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and not force:
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    path.write_text(content)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline-registry", type=Path)
    parser.add_argument("--baseline-dir", type=Path, action="append", default=[])
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output or args.research_dir / "snapshot_manifest.json"
    manifest = build_snapshot_manifest(args.signal_dir, args.research_dir)
    write_new_json(output, manifest, force=args.force)
    print(f"snapshot_id={manifest['snapshot_id']}")
    print(f"files={len(manifest['files'])} output={output}")

    if args.baseline_dir:
        registry_path = args.baseline_registry or output.with_name("baseline_registry.json")
        registry = build_baseline_registry(
            args.baseline_dir, manifest["code_commit"]
        )
        write_new_json(registry_path, registry, force=args.force)
        print(f"baseline_files={len(registry['files'])} output={registry_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
