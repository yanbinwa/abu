# -*- encoding: utf-8 -*-
"""Immutable, content-addressed manifests for selection research artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path


SCHEMA_VERSION = "selection_artifact_manifest_v1"
CONFIG_COMPONENTS = (
    "universe", "feature", "label", "model", "calibration",
    "portfolio", "risk", "execution",
)
DATE_RANGE_COMPONENTS = ("train", "calibration", "test")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(payload) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_payload(payload) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def sha256_file(path, chunk_size=1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def manifest_sha256(manifest) -> str:
    body = {key: value for key, value in manifest.items()
            if key != "manifest_sha256"}
    return sha256_payload(body)


def _git_info(repo_root):
    root = Path(repo_root)

    def command(*parts):
        try:
            return subprocess.check_output(
                ["git", *parts], cwd=str(root), text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return "unknown"

    status = command("status", "--porcelain=v1", "--untracked-files=all")
    return {
        "commit": command("rev-parse", "HEAD"),
        "branch": command("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status and status != "unknown"),
        "status_sha256": (
            hashlib.sha256(status.encode("utf-8")).hexdigest()
            if status != "unknown" else "unknown"
        ),
    }


def _dependency_versions(names=("numpy", "pandas", "scikit-learn")):
    versions = {}
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    return dict(sorted(versions.items()))


def runtime_environment():
    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "dependencies": _dependency_versions(),
    }


def artifact_file_record(role, path, logical_path=None):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("artifact file does not exist: {}".format(path))
    return {
        "role": str(role),
        "path": str(logical_path if logical_path is not None else path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _normalize_file_records(records):
    normalized = []
    for record in records:
        if isinstance(record, dict):
            item = dict(record)
        else:
            role, path = record
            item = artifact_file_record(role, path)
        normalized.append(item)
    return sorted(normalized, key=lambda item: (item["role"], item["path"]))


def _normalize_snapshot(data_snapshot):
    snapshot = dict(data_snapshot)
    files = list(snapshot.get("files", []))
    snapshot["files"] = sorted(
        (dict(item) for item in files), key=lambda item: item["path"]
    )
    return snapshot


def build_artifact_manifest(
        artifact_id, stage, repo_root, data_snapshot, config_hashes,
        date_ranges, input_files=(), output_files=(),
        parent_manifest_sha256=None, git=None, environment=None,
        created_at_utc=None):
    """Build and validate a content-addressed research artifact manifest."""
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": str(artifact_id),
        "stage": str(stage),
        "created_at_utc": created_at_utc or datetime.now(
            timezone.utc).isoformat(),
        "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
        "git": dict(git) if git is not None else _git_info(repo_root),
        "data_snapshot": _normalize_snapshot(data_snapshot),
        "config_hashes": dict(sorted(config_hashes.items())),
        "date_ranges": {
            key: date_ranges.get(key) for key in DATE_RANGE_COMPONENTS
        },
        "environment": (
            dict(environment) if environment is not None
            else runtime_environment()
        ),
        "input_files": _normalize_file_records(input_files),
        "output_files": _normalize_file_records(output_files),
        "parent_manifest_sha256": parent_manifest_sha256,
    }
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    validate_artifact_manifest(manifest)
    return manifest


def _is_hash_or_not_applicable(value):
    return value == "NOT_APPLICABLE" or bool(
        SHA256_PATTERN.fullmatch(str(value)))


def validate_artifact_manifest(manifest):
    required = {
        "schema_version", "artifact_id", "stage", "created_at_utc",
        "research_label", "git", "data_snapshot", "config_hashes",
        "date_ranges", "environment", "input_files", "output_files",
        "parent_manifest_sha256", "manifest_sha256",
    }
    if set(manifest) != required:
        raise ValueError("artifact manifest fields mismatch")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("artifact manifest schema mismatch")
    if manifest["research_label"] != \
            "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING":
        raise ValueError("artifact is not marked research-only")
    if not manifest["artifact_id"] or not manifest["stage"]:
        raise ValueError("artifact_id and stage are required")

    snapshot = manifest["data_snapshot"]
    if set(snapshot) != {"snapshot_id", "files"}:
        raise ValueError("data snapshot fields mismatch")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot["snapshot_id"]):
        raise ValueError("invalid data snapshot_id")
    seen_snapshot = set()
    for item in snapshot["files"]:
        if set(item) != {"path", "sha256", "bytes"}:
            raise ValueError("data snapshot file fields mismatch")
        if item["path"] in seen_snapshot:
            raise ValueError("duplicate data snapshot path")
        seen_snapshot.add(item["path"])
        if not SHA256_PATTERN.fullmatch(str(item["sha256"])):
            raise ValueError("invalid data snapshot file hash")
        if not isinstance(item["bytes"], int) or item["bytes"] < 0:
            raise ValueError("invalid data snapshot file size")

    if set(manifest["config_hashes"]) != set(CONFIG_COMPONENTS):
        raise ValueError("artifact config hash components mismatch")
    if not all(_is_hash_or_not_applicable(value)
               for value in manifest["config_hashes"].values()):
        raise ValueError("invalid artifact config hash")
    if set(manifest["date_ranges"]) != set(DATE_RANGE_COMPONENTS):
        raise ValueError("artifact date range components mismatch")
    for name, value in manifest["date_ranges"].items():
        if value is None:
            continue
        if not isinstance(value, list) or len(value) != 2 or \
                not all(isinstance(item, str) and item for item in value):
            raise ValueError("invalid date range for {}".format(name))
        if value[0] > value[1]:
            raise ValueError("reversed date range for {}".format(name))

    roles = set()
    for namespace in ("input_files", "output_files"):
        for item in manifest[namespace]:
            if set(item) != {"role", "path", "sha256", "bytes"}:
                raise ValueError("artifact file record fields mismatch")
            key = (namespace, item["role"])
            if key in roles:
                raise ValueError("duplicate artifact file role")
            roles.add(key)
            if not SHA256_PATTERN.fullmatch(str(item["sha256"])):
                raise ValueError("invalid artifact file hash")
            if not isinstance(item["bytes"], int) or item["bytes"] < 0:
                raise ValueError("invalid artifact file size")

    parent = manifest["parent_manifest_sha256"]
    if parent is not None and not SHA256_PATTERN.fullmatch(str(parent)):
        raise ValueError("invalid parent manifest hash")
    if not SHA256_PATTERN.fullmatch(str(manifest["manifest_sha256"])):
        raise ValueError("invalid manifest hash")
    if manifest["manifest_sha256"] != manifest_sha256(manifest):
        raise ValueError("artifact manifest hash mismatch")
    return manifest


def write_artifact_manifest(path, manifest):
    path = Path(path)
    validate_artifact_manifest(manifest)
    if path.exists():
        raise FileExistsError("refusing to overwrite artifact manifest: {}".format(
            path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def read_artifact_manifest(path):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_artifact_manifest(manifest)


def _verify_expected(manifest, expected):
    for key, wanted in (expected or {}).items():
        if key == "config_hashes":
            for component, value in wanted.items():
                if manifest["config_hashes"].get(component) != value:
                    raise ValueError(
                        "artifact config hash mismatch: {}".format(component))
        elif key == "data_snapshot_id":
            if manifest["data_snapshot"]["snapshot_id"] != wanted:
                raise ValueError("artifact data snapshot mismatch")
        elif manifest.get(key) != wanted:
            raise ValueError("artifact manifest expectation mismatch: {}".format(
                key))


def verify_artifact_manifest(manifest_or_path, file_overrides=None,
                             expected=None):
    """Verify manifest, expectations, and every bound input/output byte."""
    if isinstance(manifest_or_path, (str, Path)):
        manifest = read_artifact_manifest(manifest_or_path)
    else:
        manifest = validate_artifact_manifest(dict(manifest_or_path))
    _verify_expected(manifest, expected)
    overrides = {str(key): Path(value) for key, value in
                 (file_overrides or {}).items()}
    for namespace in ("input_files", "output_files"):
        for record in manifest[namespace]:
            path = overrides.get(record["role"], Path(record["path"]))
            if not path.is_file():
                raise ValueError(
                    "artifact file is missing: {} ({})".format(
                        record["role"], path))
            if path.stat().st_size != record["bytes"] or \
                    sha256_file(path) != record["sha256"]:
                raise ValueError(
                    "artifact file hash mismatch: {} ({})".format(
                        record["role"], path))
    return manifest


def prediction_manifest_path(prediction_path):
    return Path(str(Path(prediction_path)) + ".manifest.json")


def bind_prediction_manifest(
        prediction_path, artifact_id, repo_root, data_snapshot,
        config_hashes, date_ranges, source_files=(),
        parent_manifest_sha256=None, manifest_path=None,
        git=None, environment=None, created_at_utc=None):
    prediction_path = Path(prediction_path).resolve()
    manifest = build_artifact_manifest(
        artifact_id=artifact_id,
        stage="oos_prediction",
        repo_root=repo_root,
        data_snapshot=data_snapshot,
        config_hashes=config_hashes,
        date_ranges=date_ranges,
        input_files=source_files,
        output_files=[artifact_file_record("prediction", prediction_path)],
        parent_manifest_sha256=parent_manifest_sha256,
        git=git,
        environment=environment,
        created_at_utc=created_at_utc,
    )
    path = manifest_path or prediction_manifest_path(prediction_path)
    write_artifact_manifest(path, manifest)
    return manifest


def verify_prediction_artifact(prediction_path, manifest_path=None,
                               expected=None):
    path = manifest_path or prediction_manifest_path(prediction_path)
    return verify_artifact_manifest(
        path, file_overrides={"prediction": prediction_path},
        expected=expected,
    )
