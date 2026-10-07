# -*- encoding: utf-8 -*-
"""Audit and normalize long-history family OOS sources for score combiners."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from .ABuMLContracts import MLContractError, ModelSourceManifest, sha256_payload


FAMILIES = (
    "regression_trend", "price_position", "volume_structure",
    "price_volume_persistence", "kbar_shape", "vwap_price",
    "residual_overheat",
)
SHANGHAI = timezone(timedelta(hours=8))


def file_sha256(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def _asof(value, hour=16, minute=0):
    parsed = datetime.strptime(str(int(value)), "%Y%m%d")
    return parsed.replace(hour=hour, minute=minute,
                          tzinfo=SHANGHAI).isoformat()


def source_manifest_from_fold(family, fold, config_sha256):
    required = {
        "fold", "train_end", "train_label_end", "validation_start",
        "validation_end", "validation_label_end", "test_start", "test_end",
        "strict_label_non_overlap",
    }
    missing = required-set(fold)
    if missing:
        raise MLContractError("family fold missing: {}".format(
            ", ".join(sorted(missing))))
    if not fold["strict_label_non_overlap"]:
        raise MLContractError("family fold does not assert label isolation")
    manifest = ModelSourceManifest(
        manifest_id="{}-fold-{:02d}".format(family, int(fold["fold"])),
        strategy_id="alpha158_family_oos_v1",
        candidate_arm_id="family_{}".format(family),
        model_id="ridge_family_{}_v1".format(family),
        fold_id=str(int(fold["fold"])),
        feature_view_id="alpha158_family_{}_v1".format(family),
        label_contract_id="alpha158_lite_excess20_v1",
        config_sha256=config_sha256,
        train_signal_max=_asof(fold["train_end"]),
        train_label_end_max=_asof(fold["train_label_end"]),
        train_label_available_at_max=_asof(fold["train_label_end"], 16, 1),
        validation_start_asof=_asof(fold["validation_start"], 9, 0),
        validation_signal_max=_asof(fold["validation_end"]),
        validation_label_end_max=_asof(fold["validation_label_end"]),
        validation_label_available_at_max=
            _asof(fold["validation_label_end"], 16, 1),
        model_selected_at=_asof(fold["validation_label_end"], 16, 2),
        model_available_at=_asof(fold["validation_label_end"], 16, 3),
        prediction_block_start=_asof(fold["test_start"], 9, 0),
        prediction_block_end=_asof(fold["test_end"], 16, 0),
    )
    return manifest


def _prediction_key_digest(path, fold_ranges=None, chunksize=250000):
    checksum = hashlib.sha256()
    rows = dates = 0
    last_key = None
    folds = set()
    for chunk in pd.read_csv(
            path, usecols=["signal_asof", "symbol", "fold"],
            dtype={"symbol": str}, chunksize=chunksize):
        keys = list(zip(chunk.signal_asof.astype(int), chunk.symbol.astype(str)))
        if len(keys) != len(set(keys)):
            raise MLContractError("duplicate OOS prediction key within chunk")
        if fold_ranges is not None:
            for fold_id, values in chunk.groupby("fold", sort=False):
                fold_id = int(fold_id)
                if fold_id not in fold_ranges:
                    raise MLContractError("prediction references unknown fold")
                lower, upper = fold_ranges[fold_id]
                if not values.signal_asof.astype(int).between(
                        lower, upper).all():
                    raise MLContractError(
                        "prediction date lies outside its OOS fold")
        if keys and last_key is not None and keys[0] <= last_key:
            raise MLContractError("family OOS keys are not globally ordered/unique")
        if keys and any(left >= right for left, right in zip(keys, keys[1:])):
            raise MLContractError("family OOS keys are not strictly ordered")
        for date, symbol in keys:
            checksum.update("{}\t{}\n".format(date, symbol).encode("utf-8"))
        if keys:
            last_key = keys[-1]
        rows += len(chunk)
        dates += chunk.signal_asof.nunique()
        folds.update(chunk.fold.astype(int).unique().tolist())
    return {"key_sha256": checksum.hexdigest(), "rows": rows,
            "fold_ids": sorted(folds), "chunk_date_count_upper_bound": dates}


def audit_family(directory, family):
    directory = Path(directory)
    prediction_path = directory/"oos_predictions.csv.gz"
    fold_path = directory/"fold_manifests.json"
    completion_path = directory/"completion.json"
    if not all(path.exists() for path in (
            prediction_path, fold_path, completion_path)):
        return {"family": family, "status": "INCOMPLETE"}
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    prediction_sha = file_sha256(prediction_path)
    if completion.get("status") != "COMPLETE" or \
            completion.get("prediction_sha256") != prediction_sha:
        raise MLContractError("family completion/hash mismatch: " + family)
    folds = json.loads(fold_path.read_text(encoding="utf-8"))
    if not folds:
        raise MLContractError("family has no folds: " + family)
    first = folds[0]
    config_sha = sha256_payload({
        "family": family,
        "label_horizon_sessions": first.get("label_horizon_sessions"),
        "additional_embargo_sessions":
            first.get("additional_embargo_sessions"),
        "baseline_model": first.get("baseline", {}).get("model"),
        "baseline_features": first.get("baseline", {}).get("features"),
        "candidate_model": first.get("candidate", {}).get("model"),
        "candidate_features": first.get("candidate", {}).get("features"),
    })
    manifests = [source_manifest_from_fold(family, fold, config_sha)
                 for fold in folds]
    fold_ranges = {int(fold["fold"]):
                   (int(fold["test_start"]), int(fold["test_end"]))
                   for fold in folds}
    prediction = _prediction_key_digest(
        prediction_path, fold_ranges=fold_ranges)
    expected_folds = sorted(int(fold["fold"]) for fold in folds)
    if prediction["fold_ids"] != expected_folds:
        raise MLContractError("family prediction folds differ from manifests")
    if int(completion.get("rows", -1)) != prediction["rows"]:
        raise MLContractError("family completion row count mismatch")
    return {
        "family": family, "status": "PASS",
        "prediction_sha256": prediction_sha,
        "fold_manifests_sha256": file_sha256(fold_path),
        "key_sha256": prediction["key_sha256"],
        "rows": prediction["rows"], "fold_ids": expected_folds,
        "source_manifests": [manifest.payload() | {
            "manifest_sha256": manifest.manifest_sha256}
            for manifest in manifests],
    }


def audit_family_set(root, dataset_report, families=FAMILIES):
    dataset = json.loads(Path(dataset_report).read_text(encoding="utf-8"))
    if dataset.get("status") != "COMPLETE" or dataset.get("missing_signals"):
        raise MLContractError("long-history dataset is incomplete")
    results = [audit_family(Path(root)/"families"/family, family)
               for family in families]
    incomplete = [row["family"] for row in results if row["status"] != "PASS"]
    if incomplete:
        return {"status": "INCOMPLETE", "incomplete_families": incomplete,
                "families": results}
    key_hashes = {row["key_sha256"] for row in results}
    row_counts = {row["rows"] for row in results}
    if len(key_hashes) != 1 or len(row_counts) != 1:
        raise MLContractError("seven family expected prediction keys differ")
    return {
        "status": "PASS", "incomplete_families": [],
        "common_key_sha256": results[0]["key_sha256"],
        "common_rows": results[0]["rows"], "families": results,
    }
