# -*- encoding: utf-8 -*-
"""Immutable forward-shadow genesis gated by historical-screen decisions."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .ABuMLContracts import (
    ExperimentDecision, ExperimentRegistration, MLContractError,
    sha256_payload,
)
from .ABuMLFamilyOOSAudit import file_sha256


def _artifact_hashes(paths):
    return {str(Path(path).resolve()): file_sha256(path)
            for path in sorted(map(Path, paths), key=lambda item: str(item))}


def build_shadow_genesis(registration, decision, artifact_paths,
                         initial_cash=1_000_000.0):
    if not isinstance(registration, ExperimentRegistration):
        raise TypeError("registration must be ExperimentRegistration")
    if not isinstance(decision, ExperimentDecision):
        raise TypeError("decision must be ExperimentDecision")
    if decision.state != "PASS_HISTORICAL_SCREEN":
        raise MLContractError("only a historical-screen pass may register shadow")
    if decision.experiment_id != registration.experiment_id or \
            decision.baseline_arm_id != registration.baseline_arm_id or \
            decision.candidate_arm_id not in registration.candidate_arm_ids:
        raise MLContractError("shadow decision identity differs from registration")
    if initial_cash <= 0:
        raise MLContractError("shadow initial cash must be positive")
    artifacts = _artifact_hashes(artifact_paths)
    payload = {
        "schema_version": "alpha158_ml_shadow_genesis_v1",
        "experiment_id": registration.experiment_id,
        "baseline_arm_id": registration.baseline_arm_id,
        "candidate_arm_id": decision.candidate_arm_id,
        "decision_evidence_sha256": decision.evidence_sha256,
        "registration_config_sha256": registration.config_sha256,
        "data_manifest_sha256": registration.data_manifest_sha256,
        "expected_prediction_keys_sha256":
            registration.expected_prediction_keys_sha256,
        "artifact_hashes": artifacts,
        "accounts": {
            "baseline": {"arm_id": registration.baseline_arm_id,
                         "initial_cash": float(initial_cash),
                         "positions": [], "orders": []},
            "candidate": {"arm_id": decision.candidate_arm_id,
                          "initial_cash": float(initial_cash),
                          "positions": [], "orders": []},
        },
        "protocol": {
            "check_months": [6, 9, 12, 18],
            "minimum_buy_date_clusters": 30,
            "missed_session_backfill": False,
            "automatic_live_admission": False,
            "formal_order_channel_enabled": False,
            "trade_notification_enabled": False,
        },
    }
    payload["genesis_sha256"] = sha256_payload(payload)
    return payload


def write_shadow_genesis(path, payload):
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix+".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2,
                      sort_keys=True, allow_nan=False)+"\n"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(str(destination.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def verify_shadow_genesis(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = payload.pop("genesis_sha256", None)
    if expected != sha256_payload(payload):
        raise MLContractError("shadow genesis hash mismatch")
    for artifact, expected_hash in payload["artifact_hashes"].items():
        if not Path(artifact).exists() or file_sha256(artifact) != expected_hash:
            raise MLContractError("shadow artifact changed: " + artifact)
    payload["genesis_sha256"] = expected
    return payload

