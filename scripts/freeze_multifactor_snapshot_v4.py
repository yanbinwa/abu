#!/usr/bin/env python3
"""Validate the frozen v4 experiment catalog and observed baselines.

This command is deliberately read-only.  It does not run a strategy or emit
returns; it only proves that the pre-registration and the already-observed
v1/v2/v3 artifacts still match the hashes frozen at M0.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = (
    ROOT / "configs/selection/alpha158_multifactor_baseline_v4.json"
)
DEFAULT_CATALOG = (
    ROOT / "configs/selection/alpha158_multifactor_experiments_v4.json"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(payload) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_object(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def validate_experiment_catalog(path: Path) -> dict:
    payload = _read_object(path)
    required = {
        "catalog_version", "status", "registered_before_v4_returns",
        "observed_history", "output_root", "required_order",
        "experiments", "prohibitions",
    }
    if set(payload) != required:
        raise ValueError("v4 experiment catalog fields mismatch")
    if payload["status"] != "PRE_REGISTERED_RESEARCH_ONLY":
        raise ValueError("v4 experiment catalog is not pre-registered")
    if payload["registered_before_v4_returns"] is not True:
        raise ValueError("catalog must be registered before v4 returns")
    if payload["observed_history"].get("classification") != \
            "development_and_diagnostic_only":
        raise ValueError("observed history cannot be treated as holdout")

    experiments = payload["experiments"]
    identifiers = [item.get("experiment_id") for item in experiments]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("duplicate v4 experiment_id")
    if identifiers != payload["required_order"]:
        raise ValueError("v4 experiment order differs from registration")
    if [item.get("order") for item in experiments] != list(
            range(len(experiments))):
        raise ValueError("v4 experiment order indices are not contiguous")

    required_experiment = {
        "order", "experiment_id", "hypothesis", "single_change",
        "continue_gate", "failure_status", "output_template",
    }
    for item in experiments:
        if set(item) != required_experiment:
            raise ValueError(
                f"experiment fields mismatch: {item.get('experiment_id')}"
            )
        if any(not str(item[key]).strip() for key in required_experiment - {
                "order"}):
            raise ValueError(
                f"experiment has an empty field: {item['experiment_id']}"
            )
        expected = "{output_root}/" + item["experiment_id"] + "/{trial_id}"
        if item["output_template"] != expected:
            raise ValueError(
                f"experiment output can overwrite another stage: "
                f"{item['experiment_id']}"
            )
    return payload


def _verify_hash(path: Path, expected: str, label: str, errors: list[str]):
    if not SHA256_PATTERN.fullmatch(str(expected)):
        errors.append(f"invalid frozen SHA256 for {label}")
        return
    if not path.is_file():
        errors.append(f"missing frozen file for {label}: {path}")
        return
    actual = sha256_file(path)
    if actual != expected:
        errors.append(
            f"frozen file changed for {label}: expected={expected} "
            f"actual={actual} path={path}"
        )


def verify_frozen_baseline(
        repo_root: Path, baseline_path: Path = DEFAULT_BASELINE,
        catalog_path: Path = DEFAULT_CATALOG,
        verify_external: bool = True) -> dict:
    """Return a read-only verification report or raise on any mismatch."""
    repo_root = Path(repo_root).resolve()
    baseline_path = Path(baseline_path).resolve()
    catalog_path = Path(catalog_path).resolve()
    catalog = validate_experiment_catalog(catalog_path)
    baseline = _read_object(baseline_path)
    errors: list[str] = []

    commit = baseline.get("git", {}).get("commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        errors.append("baseline git commit is not a full SHA1")
    if baseline.get("observed_history", {}).get("independent_holdout") is not False:
        errors.append("observed history must not be classified as holdout")
    if baseline.get("research_label") != \
            "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING":
        errors.append("research-only label is missing")

    catalog_record = baseline.get("experiment_catalog", {})
    expected_catalog_path = repo_root / catalog_record.get("path", "")
    if expected_catalog_path.resolve() != catalog_path:
        errors.append("baseline points at a different experiment catalog")
    _verify_hash(
        catalog_path, catalog_record.get("sha256", ""),
        "experiment_catalog", errors,
    )

    frozen_versions = set()
    for item in baseline.get("frozen_configs", []):
        version = item.get("strategy_version", "")
        frozen_versions.add(version)
        path = repo_root / item.get("path", "")
        _verify_hash(path, item.get("file_sha256", ""), version, errors)
        if path.is_file():
            canonical = canonical_sha256(_read_object(path))
            if canonical != item.get("canonical_config_sha256"):
                errors.append(
                    f"canonical config changed for {version}: "
                    f"expected={item.get('canonical_config_sha256')} "
                    f"actual={canonical}"
                )
    expected_versions = {
        "alpha158_lite_v1", "alpha158_lite_turnover_v2",
        "alpha158_lite_low_turnover_v3",
    }
    if frozen_versions != expected_versions:
        errors.append("v1/v2/v3 frozen config set is incomplete")
    classifications = {
        item.get("strategy_version"): item.get("classification")
        for item in baseline.get("frozen_configs", [])
    }
    if classifications.get("alpha158_lite_low_turnover_v3") != \
            "observed_post_hoc_diagnostic":
        errors.append("v3 must remain an observed post-hoc diagnostic")

    for item in baseline.get("frozen_reviews", []):
        path = repo_root / item.get("path", "")
        _verify_hash(path, item.get("sha256", ""), item.get("path", ""),
                     errors)

    historical_roots = []
    for artifact in baseline.get("historical_artifacts", []):
        root = Path(artifact.get("root", "")).resolve()
        historical_roots.append(root)
        if not verify_external:
            continue
        for relative, expected in artifact.get("files", {}).items():
            _verify_hash(
                root / relative, expected,
                f"{artifact.get('strategy_version')}/{relative}", errors,
            )

    v4_root = Path(baseline.get("v4_output_root", "")).resolve()
    if str(v4_root) != str(Path(catalog["output_root"]).resolve()):
        errors.append("catalog and baseline use different v4 output roots")
    if v4_root in historical_roots:
        errors.append("v4 output root collides with a frozen historical root")
    for historical_root in historical_roots:
        if v4_root.is_relative_to(historical_root) or \
                historical_root.is_relative_to(v4_root):
            errors.append(
                f"v4 and historical output roots overlap: {historical_root}"
            )

    if errors:
        raise ValueError("\n".join(errors))
    return {
        "baseline_id": baseline.get("baseline_id"),
        "catalog_version": catalog["catalog_version"],
        "experiment_ids": catalog["required_order"],
        "frozen_strategy_versions": sorted(frozen_versions),
        "external_artifacts_verified": bool(verify_external),
        "v4_output_root": str(v4_root),
        "status": "VERIFIED_RESEARCH_ONLY",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument(
        "--skip-external", action="store_true",
        help="verify only version-controlled files; intended for portable CI",
    )
    args = parser.parse_args()
    report = verify_frozen_baseline(
        ROOT, args.baseline, args.catalog,
        verify_external=not args.skip_external,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
