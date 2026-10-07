# -*- encoding: utf-8 -*-
"""Immutable pre-registration for machine-learning research experiments."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..AlphaBu.ABuArtifactManifest import sha256_file, sha256_payload
from .ABuMLContracts import (
    ExperimentRegistration, MLContractError, canonical_config_sha256,
    validate_experiment_config,
)


def expected_prediction_keys_sha256(keys):
    normalized = sorted([list(item) for item in keys])
    return sha256_payload(normalized)


def _atomic_write_new(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(str(path))
    temporary = path.with_name(".{}.{}.tmp".format(path.name, os.getpid()))
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def register_experiment(output_dir, config, data_manifest_sha256,
                        expected_prediction_keys, protected_paths=(),
                        created_at=None, model_registry=None):
    validate_experiment_config(config, model_registry=model_registry)
    if len(str(data_manifest_sha256)) != 64:
        raise MLContractError("data manifest hash is invalid")
    protected = []
    for item in sorted(Path(path).resolve() for path in protected_paths):
        if not item.is_file():
            raise MLContractError("protected file does not exist: {}".format(item))
        protected.append((str(item), sha256_file(item)))
    registration = ExperimentRegistration(
        experiment_id=config["experiment_id"],
        experiment_family_id=config["experiment_family_id"],
        experiment_type=config["experiment_type"],
        baseline_arm_id=config["baseline_arm_id"],
        candidate_arm_ids=tuple(config["candidate_arm_ids"]),
        config_sha256=canonical_config_sha256(config),
        data_manifest_sha256=str(data_manifest_sha256),
        expected_prediction_keys_sha256=expected_prediction_keys_sha256(
            expected_prediction_keys),
        protected_file_hashes=tuple(protected),
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
    )
    payload = registration.payload()
    payload["registration_sha256"] = sha256_payload(payload)
    _atomic_write_new(Path(output_dir) / "registration.json", payload)
    return payload


def audit_registration(output_dir, config, data_manifest_sha256,
                       expected_prediction_keys):
    path = Path(output_dir) / "registration.json"
    if not path.is_file():
        raise MLContractError("registration is missing")
    with path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    digest = payload.pop("registration_sha256", None)
    if digest != sha256_payload(payload):
        raise MLContractError("registration hash mismatch")
    if payload["config_sha256"] != canonical_config_sha256(config):
        raise MLContractError("registered config changed")
    if payload["data_manifest_sha256"] != str(data_manifest_sha256):
        raise MLContractError("registered data manifest changed")
    expected_hash = expected_prediction_keys_sha256(expected_prediction_keys)
    if payload["expected_prediction_keys_sha256"] != expected_hash:
        raise MLContractError("expected prediction keys changed")
    for protected_path, expected in payload["protected_file_hashes"]:
        path = Path(protected_path)
        if not path.is_file() or sha256_file(path) != expected:
            raise MLContractError("protected file changed: {}".format(path))
    payload["registration_sha256"] = digest
    return payload
