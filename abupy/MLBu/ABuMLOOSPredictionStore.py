# -*- encoding: utf-8 -*-
"""Fail-closed store for complete, recursively-audited OOS predictions."""
from __future__ import annotations

import json
from pathlib import Path

from ..AlphaBu.ABuArtifactManifest import sha256_payload
from .ABuMLContracts import (
    MLContractError, ModelSourceManifest, OOSPredictionRecord, parse_asof,
)


class IncompletePredictionCoverage(MLContractError):
    pass


class ModelSourceManifestCatalog(object):

    def __init__(self):
        self._manifests = {}

    def add(self, manifest):
        if not isinstance(manifest, ModelSourceManifest):
            raise TypeError("manifest must be ModelSourceManifest")
        if manifest.manifest_id in self._manifests:
            raise MLContractError("duplicate source manifest id")
        for existing in self._manifests.values():
            same_stream = (existing.strategy_id == manifest.strategy_id and
                           existing.candidate_arm_id == manifest.candidate_arm_id and
                           existing.model_id == manifest.model_id)
            if not same_stream:
                continue
            left_start = parse_asof(existing.prediction_block_start)
            left_end = parse_asof(existing.prediction_block_end)
            right_start = parse_asof(manifest.prediction_block_start)
            right_end = parse_asof(manifest.prediction_block_end)
            if max(left_start, right_start) <= min(left_end, right_end):
                raise MLContractError("prediction folds overlap")
        self._manifests[manifest.manifest_id] = manifest
        return manifest

    def get(self, manifest_id):
        if manifest_id not in self._manifests:
            raise MLContractError("source manifest is missing: {}".format(manifest_id))
        return self._manifests[manifest_id]

    def audit(self, manifest_id, visiting=None):
        visiting = set() if visiting is None else visiting
        if manifest_id in visiting:
            raise MLContractError("source manifest cycle detected")
        manifest = self.get(manifest_id)
        visiting.add(manifest_id)
        for parent_id, expected_hash in zip(
                manifest.parent_manifest_ids, manifest.parent_manifest_hashes):
            parent = self.get(parent_id)
            if parent.manifest_sha256 != expected_hash:
                raise MLContractError("parent manifest hash mismatch")
            self.audit(parent_id, visiting)
        visiting.remove(manifest_id)
        return manifest


class MLOOSPredictionStore(object):

    def __init__(self, expected_keys, manifest_catalog):
        materialized = [tuple(item) for item in expected_keys]
        self.expected_keys = frozenset(materialized)
        if len(self.expected_keys) != len(materialized):
            raise MLContractError("expected prediction keys contain duplicates")
        self.catalog = manifest_catalog
        self._records = {}

    def add(self, record):
        if not isinstance(record, OOSPredictionRecord):
            raise TypeError("record must be OOSPredictionRecord")
        if record.key not in self.expected_keys:
            raise MLContractError("unexpected OOS prediction key")
        if record.key in self._records:
            raise MLContractError("duplicate OOS prediction key")
        manifest = self.catalog.audit(record.source_manifest_id)
        matching = (
            manifest.strategy_id == record.strategy_id and
            manifest.candidate_arm_id == record.candidate_arm_id and
            manifest.model_id == record.model_id and
            manifest.fold_id == record.fold_id and
            manifest.feature_view_id == record.feature_view_id and
            manifest.label_contract_id == record.label_contract_id
        )
        if not matching:
            raise MLContractError("prediction does not match source manifest")
        signal = parse_asof(record.signal_asof)
        if not (parse_asof(manifest.model_available_at) <= signal <=
                parse_asof(manifest.prediction_block_end)):
            raise MLContractError("model was unavailable at prediction time")
        self._records[record.key] = record
        return record

    @property
    def missing_keys(self):
        return self.expected_keys - set(self._records)

    def finalize(self):
        actual = set(self._records)
        if actual != self.expected_keys:
            raise IncompletePredictionCoverage(
                "actual OOS keys differ from preregistered expected keys")
        return [self._records[key] for key in sorted(self._records)]

    def write_jsonl(self, path):
        records = self.finalize()
        path = Path(path)
        if path.exists():
            raise FileExistsError(str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(record.payload(), ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"))
                 for record in records]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return sha256_payload([record.payload() for record in records])
