#!/usr/bin/env python3
"""Publish an immutable, minimal paper-service runtime outside protected folders."""
from __future__ import absolute_import

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_head(root=ROOT):
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(root), text=True).strip()


def _copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(destination))


def _inventory(root):
    result = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "runtime_manifest.json":
            continue
        result.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        })
    return result


def freeze_runtime(output_root, commit=None, source_root=ROOT):
    source_root = Path(source_root).resolve()
    output_root = Path(output_root).resolve()
    commit = commit or git_head(source_root)
    release = output_root / "releases" / commit
    if release.exists():
        manifest_path = release / "runtime_manifest.json"
        if not manifest_path.exists():
            raise ValueError("existing runtime release has no manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        actual = _inventory(release)
        if actual != manifest["files"]:
            raise ValueError("existing runtime release failed hash verification")
        return release, manifest, False

    output_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".runtime-", dir=str(output_root)))
    try:
        package = stage / "abupy"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(
            "# Minimal frozen package for ServiceBu only.\n", encoding="utf-8")
        source_service = source_root / "abupy" / "ServiceBu"
        for path in source_service.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                _copy_file(path, stage / "abupy" / "ServiceBu" /
                           path.relative_to(source_service))
        _copy_file(source_root / "scripts" / "run_abu_service.py",
                   stage / "scripts" / "run_abu_service.py")
        for path in (source_root / "configs" / "service").rglob("*"):
            if path.is_file():
                _copy_file(path, stage / "configs" / "service" /
                           path.relative_to(source_root / "configs" / "service"))

        service_path = stage / "configs" / "service" / "service_v1.json"
        service = json.loads(service_path.read_text(encoding="utf-8"))
        service["daily_data_policy_path"] = str(
            release / "configs" / "service" / "daily_data_v1.json")
        service["daily_source_config_path"] = str(
            release / "configs" / "service" / "daily_sources_v1.json")
        service_path.write_text(json.dumps(
            service, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        manifest = {
            "schema_version": "paper_service_runtime_manifest_v1",
            "source_commit": commit,
            "minimal_abupy_package": True,
            "files": _inventory(stage),
        }
        (stage / "runtime_manifest.json").write_text(json.dumps(
            manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        release.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(stage), str(release))
        directory = os.open(str(release.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return release, manifest, True
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path,
                        default=Path("/Users/wjy/abu/service/paper_v1/runtime"))
    parser.add_argument("--commit")
    return parser.parse_args()


def main():
    args = parse_args()
    release, manifest, created = freeze_runtime(args.output_root, args.commit)
    print(json.dumps({
        "release": str(release), "source_commit": manifest["source_commit"],
        "file_count": len(manifest["files"]), "created": created,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
