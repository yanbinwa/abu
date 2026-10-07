# -*- encoding: utf-8 -*-
"""Frozen contracts for the Alpha158 machine-learning research pipeline."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from importlib import metadata
from pathlib import Path

from jsonschema import Draft202012Validator

from ..AlphaBu.ABuArtifactManifest import canonical_json, sha256_payload


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_PATH = REPO_ROOT / "configs/ml/ml_experiment_v1.schema.json"
DEFAULT_MODEL_REGISTRY_PATH = REPO_ROOT / "configs/ml/model_registry_v1.json"
SCHEMA_VERSION = "alpha158_ml_experiment_v1"


class MLContractError(ValueError):
    """Raised when a frozen research contract is incomplete or inconsistent."""


class MLDependencyUnavailable(RuntimeError):
    """Raised when a registered model dependency is unavailable."""


def parse_asof(value):
    """Parse a timezone-aware ISO timestamp used by leakage contracts."""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MLContractError("as-of timestamps must include a timezone")
    return parsed


@dataclass(frozen=True)
class FeatureView:
    feature_view_id: str
    columns: tuple

    def __post_init__(self):
        if not self.feature_view_id or not self.columns:
            raise MLContractError("feature view id and columns are required")
        if len(self.columns) != len(set(self.columns)):
            raise MLContractError("feature columns must be unique")


@dataclass(frozen=True)
class LabelContract:
    label_contract_id: str
    horizon_sessions: int

    def __post_init__(self):
        if not self.label_contract_id or self.horizon_sessions <= 0:
            raise MLContractError("invalid label contract")


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    plugin: str
    parameters_sha256: str


@dataclass(frozen=True)
class FoldManifest:
    fold_id: str
    train_start: str
    validation_start: str
    prediction_block_start: str
    prediction_block_end: str

    def __post_init__(self):
        values = [parse_asof(getattr(self, name)) for name in (
            "train_start", "validation_start", "prediction_block_start",
            "prediction_block_end")]
        if not values[0] < values[1] < values[2] <= values[3]:
            raise MLContractError("fold ranges must be ordered")


@dataclass(frozen=True)
class ModelSourceManifest:
    manifest_id: str
    strategy_id: str
    candidate_arm_id: str
    model_id: str
    fold_id: str
    feature_view_id: str
    label_contract_id: str
    config_sha256: str
    train_signal_max: str
    train_label_end_max: str
    train_label_available_at_max: str
    validation_start_asof: str
    validation_signal_max: str
    validation_label_end_max: str
    validation_label_available_at_max: str
    model_selected_at: str
    model_available_at: str
    prediction_block_start: str
    prediction_block_end: str
    parent_manifest_ids: tuple = ()
    parent_manifest_hashes: tuple = ()

    def __post_init__(self):
        if not all((self.manifest_id, self.strategy_id,
                    self.candidate_arm_id, self.model_id, self.fold_id,
                    self.feature_view_id, self.label_contract_id)):
            raise MLContractError("source manifest identity fields are required")
        if len(self.config_sha256) != 64:
            raise MLContractError("source manifest config hash is invalid")
        if len(self.parent_manifest_ids) != len(self.parent_manifest_hashes):
            raise MLContractError("parent manifest ids and hashes differ")
        train_signal = parse_asof(self.train_signal_max)
        train_end = parse_asof(self.train_label_end_max)
        train_available = parse_asof(self.train_label_available_at_max)
        validation_start = parse_asof(self.validation_start_asof)
        validation_signal = parse_asof(self.validation_signal_max)
        validation_end = parse_asof(self.validation_label_end_max)
        validation_available = parse_asof(
            self.validation_label_available_at_max)
        selected = parse_asof(self.model_selected_at)
        available = parse_asof(self.model_available_at)
        prediction_start = parse_asof(self.prediction_block_start)
        prediction_end = parse_asof(self.prediction_block_end)
        if not train_signal <= train_end <= train_available < validation_start:
            raise MLContractError("training labels are not mature before validation")
        if not validation_signal <= validation_end <= validation_available:
            raise MLContractError("validation label chronology is invalid")
        if not validation_available <= selected <= available <= prediction_start:
            raise MLContractError(
                "validation selection is not mature before prediction")
        if prediction_start > prediction_end:
            raise MLContractError("prediction block is reversed")

    def payload(self):
        payload = asdict(self)
        payload["parent_manifest_ids"] = list(self.parent_manifest_ids)
        payload["parent_manifest_hashes"] = list(self.parent_manifest_hashes)
        return payload

    @property
    def manifest_sha256(self):
        return sha256_payload(self.payload())


@dataclass(frozen=True)
class OOSPredictionRecord:
    strategy_id: str
    signal_asof: str
    symbol: str
    score: float
    model_id: str
    fold_id: str
    candidate_arm_id: str
    source_manifest_id: str
    feature_view_id: str
    label_contract_id: str

    @property
    def key(self):
        return (self.signal_asof, self.symbol, self.fold_id,
                self.candidate_arm_id)

    def payload(self):
        return asdict(self)


@dataclass(frozen=True)
class ExperimentRegistration:
    experiment_id: str
    experiment_family_id: str
    experiment_type: str
    baseline_arm_id: str
    candidate_arm_ids: tuple
    config_sha256: str
    data_manifest_sha256: str
    expected_prediction_keys_sha256: str
    protected_file_hashes: tuple
    created_at: str

    def payload(self):
        payload = asdict(self)
        payload["candidate_arm_ids"] = list(self.candidate_arm_ids)
        payload["protected_file_hashes"] = [list(item)
                                              for item in self.protected_file_hashes]
        return payload


@dataclass(frozen=True)
class ExperimentDecision:
    experiment_id: str
    candidate_arm_id: str
    baseline_arm_id: str
    state: str
    evidence_sha256: str


def read_json(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def canonical_config_sha256(payload):
    """Return the stable SHA256 of a JSON-compatible configuration."""
    return sha256_payload(payload)


def _format_schema_errors(validator, payload):
    errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
    if not errors:
        return None
    first = errors[0]
    location = ".".join(str(part) for part in first.absolute_path) or "<root>"
    return "{}: {}".format(location, first.message)


def validate_model_registry(payload):
    if set(payload) != {"schema_version", "models"}:
        raise MLContractError("model registry fields mismatch")
    if payload["schema_version"] != "alpha158_ml_model_registry_v1":
        raise MLContractError("model registry schema mismatch")
    models = payload["models"]
    if not isinstance(models, dict) or not models:
        raise MLContractError("model registry must contain models")
    required = {"plugin", "dependency", "version", "fallback", "parameters"}
    for model_id, model in models.items():
        if not model_id or set(model) != required:
            raise MLContractError("invalid model registry entry: {}".format(model_id))
        if model["fallback"] is not None:
            raise MLContractError("model fallback is forbidden: {}".format(model_id))
        if not isinstance(model["parameters"], dict):
            raise MLContractError("model parameters must be an object")
    return payload


def load_model_registry(path=DEFAULT_MODEL_REGISTRY_PATH):
    return validate_model_registry(read_json(path))


def validate_experiment_config(payload, schema_path=DEFAULT_SCHEMA_PATH,
                               model_registry=None):
    """Validate JSON shape plus cross-field experiment semantics."""
    schema = read_json(schema_path)
    validator = Draft202012Validator(schema)
    message = _format_schema_errors(validator, payload)
    if message:
        raise MLContractError("experiment schema violation: {}".format(message))
    if payload["schema_version"] != SCHEMA_VERSION:
        raise MLContractError("experiment schema version mismatch")

    expected_baseline = {
        "score_replacement": "ridge_26f_v1",
        "score_combiner": "all_mean_rank_a0",
    }[payload["experiment_type"]]
    if payload["baseline_arm_id"] != expected_baseline:
        raise MLContractError(
            "{} requires baseline_arm_id={}".format(
                payload["experiment_type"], expected_baseline))
    if payload["baseline_arm_id"] in payload["candidate_arm_ids"]:
        raise MLContractError("baseline arm cannot also be a candidate")
    if len(payload["candidate_arm_ids"]) != len(payload["candidate_models"]):
        raise MLContractError("candidate arm/model cardinality mismatch")

    registry = (validate_model_registry(model_registry)
                if model_registry is not None else load_model_registry())
    registered = set(registry["models"])
    requested = {payload["baseline_model"], *payload["candidate_models"]}
    missing = sorted(requested - registered)
    if missing:
        raise MLContractError("unregistered models: {}".format(", ".join(missing)))

    costs = [item["slippage_bp_per_side"] for item in payload["cost_scenarios"]]
    if costs != [25, 40, 60]:
        raise MLContractError("cost scenarios must be ordered 25/40/60bp")
    return payload


def load_experiment_config(path, schema_path=DEFAULT_SCHEMA_PATH,
                           model_registry=None):
    payload = read_json(path)
    validate_experiment_config(payload, schema_path, model_registry)
    return payload


def dependency_versions(names=("numpy", "pandas", "scikit-learn", "scipy",
                               "jsonschema", "lightgbm")):
    versions = {}
    for name in names:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    return dict(sorted(versions.items()))


def assert_model_dependencies(model_ids, model_registry=None, versions=None):
    """Fail closed when a model dependency is missing or version-mismatched."""
    registry = (validate_model_registry(model_registry)
                if model_registry is not None else load_model_registry())
    installed = versions or dependency_versions()
    for model_id in model_ids:
        if model_id not in registry["models"]:
            raise MLDependencyUnavailable("unregistered model: {}".format(model_id))
        spec = registry["models"][model_id]
        dependency = spec["dependency"]
        expected = spec["version"]
        actual = installed.get(dependency, "not-installed")
        if actual == "not-installed":
            raise MLDependencyUnavailable(
                "{} requires {} {}; dependency is not installed".format(
                    model_id, dependency, expected))
        if expected != "runtime" and actual != expected:
            raise MLDependencyUnavailable(
                "{} requires {} {}, found {}".format(
                    model_id, dependency, expected, actual))
    return True


def normalized_config_text(payload):
    """Expose the exact canonical representation covered by the config hash."""
    return canonical_json(payload)
