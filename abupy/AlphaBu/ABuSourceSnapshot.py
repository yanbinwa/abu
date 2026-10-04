# -*- encoding: utf-8 -*-
"""Reproducible source snapshots for dirty-worktree research runs."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


SOURCE_SNAPSHOT_SCHEMA_VERSION = "abu_source_snapshot_v1"


def _git(root, *args, binary=False):
    return subprocess.check_output(
        ["git", *args], cwd=str(root),
        text=not binary, stderr=subprocess.DEVNULL,
    )


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _null_paths(value):
    return sorted(item for item in value.split("\0") if item)


def build_source_snapshot(repo_root):
    """Describe the exact checked-out bytes, including dirty and untracked files."""
    root = Path(repo_root).resolve()
    if not (root / ".git").exists():
        raise ValueError("repo_root is not a git checkout")
    tracked = _null_paths(_git(root, "ls-files", "-z"))
    untracked = _null_paths(_git(
        root, "ls-files", "--others", "--exclude-standard", "-z"))
    paths = sorted(set(tracked + untracked))
    files = []
    for logical in paths:
        path = root / logical
        if not path.is_file():
            continue
        files.append({
            "path": logical,
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
            "tracked": logical in tracked,
        })
    diff = _git(root, "diff", "--binary", "HEAD", "--", binary=True)
    status = _git(
        root, "status", "--porcelain=v1", "--untracked-files=all")
    payload = {
        "schema_version": SOURCE_SNAPSHOT_SCHEMA_VERSION,
        "commit": _git(root, "rev-parse", "HEAD").strip(),
        "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip(),
        "dirty": bool(status.strip()),
        "status_sha256": _sha256_bytes(status.encode("utf-8")),
        "tracked_diff_sha256": _sha256_bytes(diff),
        "untracked_files": [item for item in files if not item["tracked"]],
        "files": files,
    }
    identity = dict(payload)
    identity.pop("untracked_files")
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["source_snapshot_hash"] = _sha256_bytes(canonical)
    return payload


def write_source_snapshot(path, snapshot):
    """Write a snapshot once; existing evidence is never overwritten."""
    target = Path(path)
    if target.exists():
        raise FileExistsError("refusing to overwrite source snapshot")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target
