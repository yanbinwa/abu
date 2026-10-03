# -*- encoding: utf-8 -*-
"""Availability evidence and immutable storage for short-line event sources."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class ShortLineSnapshotMeta:
    source: str
    dataset: str
    query_date: int
    ingested_at: str
    availability_evidence: str
    status: str
    row_count: int
    schema_sha256: str
    payload_sha256: str
    error_code: str = ""
    error_message: str = ""

    @property
    def asof_feature_allowed(self):
        return (self.availability_evidence == "FORWARD_CAPTURE" and
                self.status == "success")

    def to_dict(self):
        result = asdict(self)
        result["asof_feature_allowed"] = self.asof_feature_allowed
        return result


@dataclass(frozen=True)
class ThemeTaxonomyMapping:
    """One bitemporal source-theme to canonical-theme relationship."""

    source: str
    source_theme_id: str
    source_theme_name: str
    canonical_theme_id: str
    canonical_theme_name: str
    taxonomy_version: str
    valid_from: int
    valid_to: int
    mapping_available_at: str
    relation: str = "alias"

    def to_dict(self):
        return asdict(self)


def availability_evidence(query_date, ingested_date):
    return ("FORWARD_CAPTURE" if int(query_date) == int(ingested_date)
            else "BACKFILLED_QUERY")


def stable_schema_hash(columns):
    return hashlib.sha256(json.dumps(
        [str(item) for item in columns], ensure_ascii=False,
        separators=(",", ":")
    ).encode()).hexdigest()


def stable_payload_hash(records):
    return hashlib.sha256(json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode()).hexdigest()


def json_safe_records(frame):
    """Return stable JSON records without turning missing values into strings."""
    if frame is None:
        return []
    clean = frame.astype(object).where(pd.notna(frame), None)
    records = clean.to_dict("records")
    return json.loads(json.dumps(
        records, ensure_ascii=False, default=str, allow_nan=False))


def classify_probe_result(frame, *, source, dataset, query_date,
                          ingested_at, ingested_date):
    """An empty response remains ambiguous and is never a zero-event fact."""
    columns = list(frame.columns) if frame is not None else []
    records = frame.to_dict("records") if frame is not None else []
    status = "success" if len(records) else "empty_ambiguous"
    return ShortLineSnapshotMeta(
        source=source, dataset=dataset, query_date=int(query_date),
        ingested_at=str(ingested_at),
        availability_evidence=availability_evidence(query_date, ingested_date),
        status=status, row_count=len(records),
        schema_sha256=stable_schema_hash(columns),
        payload_sha256=stable_payload_hash(records),
    )


def _write_new(path, content):
    """Create a file exactly once and fsync it before publishing metadata."""
    path = Path(path)
    with path.open("x", encoding="utf-8", newline="") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _batch_name(ingested_at, nonce):
    stamp = re.sub(r"[^0-9]", "", str(ingested_at))[:20]
    return "{}_{}".format(stamp, re.sub(r"[^A-Za-z0-9_-]", "", nonce))


class ImmutableShortLineSnapshotStore:
    """Write append-only provider frames and traceable normalized snapshots.

    A repeated provider payload still gets its own immutable raw capture and
    metadata.  Its normalized output points at the first identical successful
    batch, so downstream assembly cannot accidentally count the same payload
    twice.
    """

    STORE_VERSION = "shortline_snapshot_store_v1"

    def __init__(self, root):
        self.root = Path(root)

    def _dataset_root(self, trade_date, dataset):
        return self.root / str(int(trade_date)) / str(dataset)

    def _prior_normalized(self, trade_date, dataset, payload_sha256):
        dataset_root = self._dataset_root(trade_date, dataset)
        if not dataset_root.exists():
            return None
        for meta_path in sorted(dataset_root.glob("*/metadata.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (meta.get("payload_sha256") == payload_sha256 and
                    meta.get("normalization_status") == "written"):
                candidate = meta.get("normalized_path")
                if candidate and (self.root / candidate).exists():
                    return candidate
        return None

    def write(self, frame, *, source, dataset, trade_date, ingested_at,
              nonce, required_columns=(), normalized=None, phase="close",
              source_semantics="provider_event_pool",
              quality_codes=(), strategy_feature_allowed=None, error=None):
        records = json_safe_records(frame)
        columns = list(frame.columns) if frame is not None else []
        status = "success" if records else "empty_ambiguous"
        error_code = ""
        error_message = ""
        if error is not None:
            status = "error"
            error_code = getattr(error, "error_code", "SOURCE_REQUEST_FAILED")
            error_message = "{}: {}".format(type(error).__name__, str(error)[:500])
        missing = sorted(set(required_columns) - set(columns))
        if status == "success" and missing:
            status = "schema_error"
            error_code = "REQUIRED_FIELDS_MISSING"
            error_message = "missing fields: {}".format(", ".join(missing))
        ingested = datetime.fromisoformat(str(ingested_at))
        if ingested.tzinfo is None or ingested.utcoffset() is None:
            raise ValueError("ingested_at must include a timezone")
        ingested_date = int(ingested.strftime("%Y%m%d"))
        evidence = availability_evidence(trade_date, ingested_date)
        schema_hash = stable_schema_hash(columns)
        payload_hash = stable_payload_hash(records)
        meta = ShortLineSnapshotMeta(
            source=source, dataset=dataset, query_date=int(trade_date),
            ingested_at=str(ingested_at), availability_evidence=evidence,
            status=status, row_count=len(records),
            schema_sha256=schema_hash, payload_sha256=payload_hash,
            error_code=error_code, error_message=error_message)

        batch_dir = self._dataset_root(trade_date, dataset) / _batch_name(
            ingested_at, nonce)
        batch_dir.mkdir(parents=True, exist_ok=False)
        raw_path = batch_dir / "provider_frame.json"
        _write_new(raw_path, json.dumps({
            "columns": [str(item) for item in columns], "records": records,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

        normalized_path = ""
        normalization_status = "not_eligible"
        prior = None
        if status == "success" and meta.asof_feature_allowed:
            prior = self._prior_normalized(trade_date, dataset, payload_hash)
            if prior:
                normalized_path = prior
                normalization_status = "reused"
            else:
                normalized_frame = normalized if normalized is not None else frame
                output = batch_dir / "normalized.csv"
                normalized_frame.to_csv(output, index=False)
                normalized_path = str(output.relative_to(self.root))
                normalization_status = "written"

        raw_relative = str(raw_path.relative_to(self.root))
        manifest = meta.to_dict()
        asof_allowed = bool(
            meta.asof_feature_allowed and status == "success" and not missing)
        if strategy_feature_allowed is None:
            strategy_feature_allowed = asof_allowed
        manifest.update({
            "store_version": self.STORE_VERSION,
            "phase": phase,
            "source_semantics": source_semantics,
            "quality_codes": list(quality_codes),
            "required_columns": list(required_columns),
            "missing_required_columns": missing,
            "raw_path": raw_relative,
            "normalization_status": normalization_status,
            "normalized_path": normalized_path,
            "normalized_reused_from": prior or "",
            "asof_feature_allowed": asof_allowed,
            "strategy_feature_allowed": bool(
                asof_allowed and strategy_feature_allowed),
        })
        _write_new(batch_dir / "metadata.json", json.dumps(
            manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return manifest

    def iter_metadata(self, trade_date=None):
        root = self.root if trade_date is None else self.root / str(int(trade_date))
        if not root.exists():
            return
        for path in sorted(root.glob("**/metadata.json")):
            yield json.loads(path.read_text(encoding="utf-8"))


class ThemeTaxonomyStore:
    """Read immutable taxonomy snapshots with point-in-time visibility."""

    REQUIRED_COLUMNS = tuple(ThemeTaxonomyMapping.__dataclass_fields__)

    def __init__(self, paths):
        frames = []
        for path in paths:
            frame = pd.read_csv(path, dtype=str)
            missing = set(self.REQUIRED_COLUMNS) - set(frame.columns)
            if missing:
                raise ValueError("taxonomy fields missing: {}".format(sorted(missing)))
            frames.append(frame[list(self.REQUIRED_COLUMNS)])
        self.frame = (pd.concat(frames, ignore_index=True) if frames else
                      pd.DataFrame(columns=self.REQUIRED_COLUMNS))
        for column in ("valid_from", "valid_to"):
            self.frame[column] = pd.to_numeric(
                self.frame[column], errors="raise").astype(int)
        parsed = pd.to_datetime(
            self.frame["mapping_available_at"], errors="raise", utc=True)
        self.frame["_mapping_available_at"] = parsed

    @classmethod
    def write_snapshot(cls, path, mappings):
        path = Path(path)
        rows = [item.to_dict() if isinstance(item, ThemeTaxonomyMapping)
                else dict(item) for item in mappings]
        frame = pd.DataFrame(rows, columns=cls.REQUIRED_COLUMNS)
        # Constructor performs the same validation used by readers.
        temporary = path.parent / (path.name + ".validate")
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(temporary, index=False)
        try:
            cls([temporary])
            if path.exists():
                raise FileExistsError("taxonomy snapshot is immutable: {}".format(path))
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def resolve(self, source, source_theme_id, trade_date, decision_at,
                taxonomy_version=None, retrospective=False):
        decision = pd.Timestamp(decision_at)
        if decision.tzinfo is None:
            raise ValueError("decision_at must include a timezone")
        decision_utc = decision.tz_convert("UTC")
        mask = ((self.frame.source == str(source)) &
                (self.frame.source_theme_id == str(source_theme_id)) &
                (self.frame.valid_from <= int(trade_date)) &
                (self.frame.valid_to >= int(trade_date)))
        if taxonomy_version is not None:
            mask &= self.frame.taxonomy_version.eq(str(taxonomy_version))
        if not retrospective:
            mask &= self.frame["_mapping_available_at"].le(decision_utc)
        columns = list(self.REQUIRED_COLUMNS)
        return self.frame.loc[mask, columns].copy().reset_index(drop=True)
