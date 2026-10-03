# -*- encoding: utf-8 -*-
"""Append-only, hash-chained registry for selection research trials."""
from __future__ import annotations

import hashlib
import json
import os
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
            seen.add(record.get("trial_id"))
            previous = record["record_sha256"]
    return records


def register_trial(path, trial_id, hypothesis, configuration,
                   status="REGISTERED", observed_metrics=None):
    path = Path(path)
    records = read_trial_registry(path, verify=True)
    if any(item["trial_id"] == trial_id for item in records):
        raise ValueError("trial_id already registered")
    record = {
        "trial_id": str(trial_id),
        "registered_at_utc": datetime.now(timezone.utc).isoformat(),
        "hypothesis": str(hypothesis),
        "configuration": configuration,
        "status": str(status),
        "observed_metrics": observed_metrics,
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

