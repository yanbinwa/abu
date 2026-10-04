#!/usr/bin/env python3
"""Freeze an immutable manifest for position-addition golden baselines."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(payload):
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def working_tree_identity():
    head = _git("rev-parse", "HEAD").decode().strip()
    patch = _git("diff", "--binary", "HEAD")
    untracked = _git(
        "ls-files", "--others", "--exclude-standard", "-z"
    ).decode().split("\0")
    untracked = sorted(item for item in untracked if item)
    records = []
    for name in untracked:
        path = ROOT / name
        if path.is_file():
            records.append({
                "path": name, "size": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    identity = {
        "git_commit": head,
        "tracked_patch_sha256": hashlib.sha256(patch).hexdigest(),
        "untracked_files": records,
    }
    identity["working_tree_sha256"] = canonical_sha256(identity)
    return identity


def runtime_identity():
    packages = {}
    for name in ("numpy", "pandas", "sklearn", "matplotlib"):
        try:
            module = importlib.import_module(name)
            packages[name] = getattr(module, "__version__", "unknown")
        except Exception as error:  # pragma: no cover - environment audit
            packages[name] = "unavailable:{}".format(type(error).__name__)
    return {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "packages": packages,
    }


def collect_path(label, path):
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_file():
        files = [path]
        base = path.parent
    else:
        files = sorted(item for item in path.rglob("*") if item.is_file())
        base = path
    rows = []
    for item in files:
        rows.append({
            "relative_path": str(item.relative_to(base)),
            "absolute_path": str(item),
            "size": item.stat().st_size,
            "sha256": sha256_file(item),
        })
    return {
        "label": str(label), "root": str(path), "files": rows,
        "content_sha256": canonical_sha256(rows),
    }


def build_manifest(inputs, metadata=None):
    records = [collect_path(label, path) for label, path in sorted(inputs)]
    identity = {
        "schema_version": "position_add_baseline_v1",
        "code": working_tree_identity(),
        "runtime": runtime_identity(),
        "inputs": records,
        "metadata": metadata or {},
    }
    identity["baseline_id"] = canonical_sha256(identity)
    return identity


def parse_named_path(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected LABEL=PATH")
    label, path = value.split("=", 1)
    if not label or not path:
        raise argparse.ArgumentTypeError("expected non-empty LABEL=PATH")
    return label, path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", type=parse_named_path,
                        required=True, help="repeatable LABEL=PATH")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--metadata-json", default="{}")
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError("baseline output already exists: {}".format(
            args.output_dir))
    metadata = json.loads(args.metadata_json)
    manifest = build_manifest(args.input, metadata)
    manifest["created_at"] = datetime.now(timezone.utc).isoformat()
    args.output_dir.mkdir(parents=True)
    output = args.output_dir / "baseline_manifest.json"
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(manifest["baseline_id"])


if __name__ == "__main__":
    main()
