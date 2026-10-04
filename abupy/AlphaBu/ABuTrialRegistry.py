# -*- encoding: utf-8 -*-
"""Append-only, hash-chained registry for selection research trials."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _record_hash(record):
    body = {key: value for key, value in record.items()
            if key != "record_sha256"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def read_trial_registry(path, verify=True):
    path = Path(path)
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    if verify:
        previous = "GENESIS"
        seen = set()
        for record in records:
            if record.get("trial_id") in seen:
                raise ValueError("duplicate trial_id")
            if record.get("previous_sha256") != previous:
                raise ValueError("trial registry hash chain is broken")
            if record.get("record_sha256") != _record_hash(record):
                raise ValueError("trial registry record hash mismatch")
            manifest_sha256 = record.get("manifest_sha256")
            if manifest_sha256 is not None and not re.fullmatch(
                    r"[0-9a-f]{64}", str(manifest_sha256)):
                raise ValueError("trial registry manifest hash is invalid")
            parent_trial_id = record.get("parent_trial_id")
            if parent_trial_id is not None and parent_trial_id not in seen:
                raise ValueError("trial registry parent must appear first")
            seen.add(record.get("trial_id"))
            previous = record["record_sha256"]
    return records


def register_trial(path, trial_id, hypothesis, configuration,
                   status="REGISTERED", observed_metrics=None,
                   manifest_sha256=None, parent_trial_id=None):
    path = Path(path)
    records = read_trial_registry(path, verify=True)
    if any(item["trial_id"] == trial_id for item in records):
        raise ValueError("trial_id already registered")
    if manifest_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", str(manifest_sha256)):
        raise ValueError("manifest_sha256 must be a SHA256 hex digest")
    if parent_trial_id is not None and not any(
            item["trial_id"] == parent_trial_id for item in records):
        raise ValueError("parent_trial_id is not registered")
    record = {
        "trial_id": str(trial_id),
        "registered_at_utc": datetime.now(timezone.utc).isoformat(),
        "hypothesis": str(hypothesis),
        "configuration": configuration,
        "status": str(status),
        "observed_metrics": observed_metrics,
        "manifest_sha256": manifest_sha256,
        "parent_trial_id": parent_trial_id,
        "previous_sha256": (records[-1]["record_sha256"]
                            if records else "GENESIS"),
    }
    record["record_sha256"] = _record_hash(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    content = "".join(_canonical(item) + "\n" for item in records+[record])
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
    return record
