from __future__ import absolute_import

import json
import glob
import re
import time
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .ABuContentStore import ContentAddressedStore, sha256_file, sha256_json


COMPONENT_VERSION_FIELDS = {
    "universe": "universe_version",
    "calendar": "calendar_version",
    "security_master": "security_master_version",
    "raw_price": "raw_price_version",
    "adjusted_price": "adjusted_price_version",
    "industry": "industry_version",
    "valuation": "valuation_version",
    "fundamental_pit": "fundamental_pit_version",
    "corporate_action": "corporate_action_version",
    "limit_reference": "limit_reference_version",
}
SENSITIVE_KEY = re.compile(
    r"(token|secret|password|passwd|authorization|api[_-]?key|cookie)", re.I)


def _redact(value):
    if isinstance(value, dict):
        return {str(key): ("<REDACTED>" if SENSITIVE_KEY.search(str(key))
                           else _redact(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware: {}".format(value))
    return parsed


@dataclass(frozen=True)
class DailyComponent(object):
    component_id: str
    version: str
    source: str
    available_at: str
    item_count: int
    quality_codes: tuple = ()
    pit_complete_from: int = None
    pit_complete_through: int = None

    def __post_init__(self):
        if self.component_id not in COMPONENT_VERSION_FIELDS:
            raise ValueError("unknown daily component: {}".format(self.component_id))
        if not self.version or not self.source or not self.available_at:
            raise ValueError("daily component identity is incomplete")
        if int(self.item_count) < 0:
            raise ValueError("component item_count cannot be negative")


class DailyRawArchive(object):
    """Content-addressed raw payloads plus immutable capture metadata."""

    def __init__(self, root):
        self.content = ContentAddressedStore(root)

    def capture(self, source, dataset, request, payload, *, retrieved_at,
                outcome="SUCCESS_NONEMPTY", source_version="unknown"):
        blob_path, blob_hash, unused_created = self.content.write_bytes(
            "raw-blobs", payload)
        metadata = {
            "schema_version": "daily_raw_capture_v1",
            "source": source,
            "source_version": source_version,
            "dataset": dataset,
            "request": _redact(request),
            "retrieved_at": retrieved_at,
            "outcome": outcome,
            "payload_sha256": blob_hash,
            "payload_bytes": len(payload),
            "blob_path": str(blob_path),
        }
        metadata_path, metadata_hash, created = self.content.write_json(
            "raw-captures", metadata)
        return {
            "metadata": metadata,
            "metadata_path": str(metadata_path),
            "metadata_sha256": metadata_hash,
            "created": created,
        }


class ProviderRateLimiter(object):
    """Small injectable limiter; collection policy remains provider-specific."""

    def __init__(self, requests_per_second, clock=None, sleeper=None):
        if float(requests_per_second) <= 0:
            raise ValueError("requests_per_second must be positive")
        self.minimum_interval = 1.0 / float(requests_per_second)
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.last_request_at = None
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = float(self.clock())
            if self.last_request_at is not None:
                remaining = self.minimum_interval - (now - self.last_request_at)
                if remaining > 0:
                    self.sleeper(remaining)
                    now = float(self.clock())
            self.last_request_at = now


def incremental_sessions(calendar_sessions, last_committed_session, target_session):
    return [int(value) for value in sorted(set(calendar_sessions))
            if (last_committed_session is None or int(value) > int(last_committed_session))
            and int(value) <= int(target_session)]


def normalize_daily_bars(frame, price_space):
    """Canonicalize bars while keeping signal and execution prices explicit."""
    if price_space not in {"RAW_EXECUTION", "ADJUSTED_SIGNAL"}:
        raise ValueError("unknown price space: {}".format(price_space))
    required = {"date", "symbol", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError("daily bars missing fields: {}".format(", ".join(missing)))
    result = frame.copy()
    result["symbol"] = result["symbol"].astype(str)
    result["date"] = pd.to_numeric(result["date"], errors="raise").astype(np.int64)
    numeric = ["open", "high", "low", "close", "volume"]
    if "amount" in result:
        numeric.append("amount")
    for field in numeric:
        result[field] = pd.to_numeric(result[field], errors="coerce")
    if result.duplicated(["date", "symbol"]).any():
        raise ValueError("duplicate daily bar key")
    finite_price = np.isfinite(result[["open", "high", "low", "close"]]).all(axis=1)
    valid_ohlc = ((result["high"] >= result[["open", "close", "low"]].max(axis=1)) &
                  (result["low"] <= result[["open", "close", "high"]].min(axis=1)))
    if not (finite_price & valid_ohlc).all():
        raise ValueError("invalid daily OHLC")
    result["price_space"] = price_space
    return result.sort_values(["date", "symbol"]).reset_index(drop=True)


def select_pit_records(records, trading_session, decision_cutoff):
    """Select latest known revision per symbol/field without future knowledge."""
    cutoff = _timestamp(decision_cutoff)
    eligible = []
    for record in records:
        if int(record["observation_session"]) > int(trading_session):
            continue
        if _timestamp(record["available_at"]) > cutoff:
            continue
        eligible.append(record)
    selected = {}
    for record in eligible:
        key = (record["symbol"], record["field"])
        ordering = (_timestamp(record["available_at"]), str(record["revision_id"]))
        if key not in selected or ordering > selected[key][0]:
            selected[key] = (ordering, dict(record))
    return [selected[key][1] for key in sorted(selected)]


def version_files(paths, root=None):
    root = None if root is None else Path(root).resolve()
    inventory = []
    for value in sorted({str(Path(path).resolve()) for path in paths}):
        path = Path(value)
        if not path.is_file():
            continue
        inventory.append({
            "path": (path.relative_to(root).as_posix()
                     if root is not None and path.is_relative_to(root) else str(path)),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        })
    return "sha256:" + sha256_json(inventory), inventory


def components_from_source_config(payload, available_at):
    """Version existing project data without changing or copying its files."""
    components = []
    details = {}
    for item in payload["components"]:
        paths = []
        for pattern in item.get("patterns", []):
            paths.extend(glob.glob(pattern, recursive=True))
        paths = sorted({str(Path(path).resolve()) for path in paths
                        if Path(path).is_file()})
        if not paths:
            details[item["component_id"]] = {
                "status": "MISSING", "patterns": item.get("patterns", [])}
            continue
        version, inventory = version_files(paths)
        component = DailyComponent(
            component_id=item["component_id"], version=version,
            source=item["source"], available_at=available_at,
            item_count=len(inventory),
            quality_codes=tuple(item.get("quality_codes", [])),
            pit_complete_from=item.get("pit_complete_from"),
            pit_complete_through=item.get("pit_complete_through"))
        components.append(component)
        details[item["component_id"]] = {
            "status": "AVAILABLE", "version": version,
            "file_count": len(inventory)}
    return components, details


class FieldDependencyPolicy(object):

    def __init__(self, payload):
        self.version = payload["policy_version"]
        self.components = {item["component_id"]: item
                           for item in payload["components"]}
        if len(self.components) != len(payload["components"]):
            raise ValueError("duplicate component policy")
        for component_id, item in self.components.items():
            if component_id not in COMPONENT_VERSION_FIELDS:
                raise ValueError("unknown component policy: {}".format(component_id))
            if item["role"] not in {"required", "optional", "display-only"}:
                raise ValueError("unknown component role")
            if item["role"] != "required" and not item.get("missing_policy"):
                raise ValueError("non-required component needs a versioned missing policy")
        self.strategies = {item["strategy_id"]: item
                           for item in payload.get("strategies", [])}
        if len(self.strategies) != len(payload.get("strategies", [])):
            raise ValueError("duplicate strategy dependency policy")

    @classmethod
    def from_path(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def resolve(self, available_components, strategy_id=None):
        available = set(available_components)
        required = {name for name, item in self.components.items()
                    if item["role"] == "required"}
        missing_required = sorted(required - available)
        if missing_required:
            raise ValueError("missing required daily components: {}".format(
                ", ".join(missing_required)))
        result = {
            "policy_version": self.version,
            "available_components": sorted(available),
            "missing_optional": sorted(
                name for name, item in self.components.items()
                if item["role"] == "optional" and name not in available),
            "display_only_missing": sorted(
                name for name, item in self.components.items()
                if item["role"] == "display-only" and name not in available),
        }
        if strategy_id is not None:
            strategy = self.strategies[strategy_id]
            dependencies = set(strategy["required_components"])
            missing = sorted(dependencies - available)
            if missing:
                raise ValueError(
                    "strategy {} blocked by missing components: {}".format(
                        strategy_id, ", ".join(missing)))
            result["strategy_id"] = strategy_id
            result["factor_set_version"] = strategy["factor_set_version"]
            result["required_components"] = sorted(dependencies)
        return result


class DailySnapshotBuilder(object):

    def __init__(self, catalog, dependency_policy):
        self.catalog = catalog
        self.policy = dependency_policy

    def build(self, trading_session, decision_cutoff, created_at, components,
              *, sequence_no=1, previous_snapshot_id=None,
              source_service="daily_data_center"):
        indexed = {item.component_id: item for item in components}
        if len(indexed) != len(components):
            raise ValueError("duplicate daily component")
        self.policy.resolve(indexed)
        cutoff = _timestamp(decision_cutoff)
        for component in components:
            if _timestamp(component.available_at) > cutoff:
                raise ValueError("component {} was not available at cutoff".format(
                    component.component_id))
            if (component.pit_complete_from is not None and
                    int(trading_session) < int(component.pit_complete_from)):
                raise ValueError("component {} PIT history is incomplete".format(
                    component.component_id))
            if (component.pit_complete_through is not None and
                    int(trading_session) > int(component.pit_complete_through)):
                raise ValueError("component {} PIT history is incomplete".format(
                    component.component_id))
        quality_codes = sorted({code for item in components for code in item.quality_codes})
        missing = sorted(set(COMPONENT_VERSION_FIELDS) - set(indexed))
        manifest = {
            "schema_version": "daily_snapshot_v1",
            "stream_id": "market-daily:{}".format(int(trading_session)),
            "sequence_no": int(sequence_no),
            "previous_snapshot_id": previous_snapshot_id,
            "trading_session": int(trading_session),
            "decision_cutoff": decision_cutoff,
            "created_at": created_at,
            "required_components": sorted(
                name for name, item in self.policy.components.items()
                if item["role"] == "required"),
            "missing_components": missing,
            "quality_codes": quality_codes,
        }
        for component_id, field in COMPONENT_VERSION_FIELDS.items():
            manifest[field] = (indexed[component_id].version
                               if component_id in indexed else "MISSING")
        manifest["component_lineage"] = [asdict(indexed[name]) for name in sorted(indexed)]
        # Daily schema deliberately has a closed public contract. Detailed lineage is
        # stored as a separately hashed component and referenced through its version.
        lineage = manifest.pop("component_lineage")
        unused_path, lineage_hash, unused_created = self.catalog.content.write_json(
            "daily-lineage", lineage)
        lineage_version = "sha256:" + lineage_hash
        manifest["quality_codes"] = sorted(set(
            manifest["quality_codes"] + ["LINEAGE:" + lineage_version]))
        return self.catalog.publish(
            "DAILY", manifest, source_service=source_service,
            event_type="DailySnapshotCommitted")


class FactorSnapshotBuilder(object):

    def __init__(self, catalog, dependency_policy):
        self.catalog = catalog
        self.policy = dependency_policy

    def build(self, strategy_id, trading_session, decision_cutoff, created_at,
              source_snapshot_id, available_components, factor_payload,
              *, sequence_no=1, previous_snapshot_id=None):
        source_snapshot = self.catalog.get(source_snapshot_id)
        if source_snapshot is None or source_snapshot["status"] != "COMMITTED":
            raise ValueError("factor source snapshot is not committed")
        resolved = self.policy.resolve(available_components, strategy_id=strategy_id)
        payload_path, factor_payload_sha256, unused_created = self.catalog.content.write_json(
            "factor-payloads", factor_payload)
        manifest = {
            "schema_version": "factor_snapshot_v1",
            "stream_id": "factor:{}:{}".format(strategy_id, int(trading_session)),
            "sequence_no": int(sequence_no),
            "previous_snapshot_id": previous_snapshot_id,
            "trading_session": int(trading_session),
            "decision_cutoff": decision_cutoff,
            "created_at": created_at,
            "strategy_id": strategy_id,
            "factor_set_version": resolved["factor_set_version"],
            "source_snapshot_id": source_snapshot_id,
            "required_components": resolved["required_components"],
            "factor_payload_sha256": factor_payload_sha256,
            "factor_payload_path": str(payload_path),
            "quality_codes": [],
        }
        return self.catalog.publish(
            "FACTOR", manifest, source_service="factor_snapshot_builder",
            event_type="FactorSnapshotCommitted")


def compare_selection_panels(expected, actual, fields=None):
    fields = fields or (
        "dates", "symbols", "close", "exec_open", "exec_close", "amount",
        "market_cap", "turnover", "universe_mask", "st_status_known",
        "buy_tradable_mask", "sell_tradable_mask",
    )
    findings = []
    for field in fields:
        left = np.asarray(getattr(expected, field))
        right = np.asarray(getattr(actual, field))
        if left.shape != right.shape:
            findings.append({"field": field, "reason": "SHAPE_MISMATCH",
                             "expected": list(left.shape), "actual": list(right.shape)})
            continue
        if np.issubdtype(left.dtype, np.number) and np.issubdtype(right.dtype, np.number):
            equal = np.allclose(left, right, equal_nan=True, rtol=0.0, atol=0.0)
        else:
            equal = np.array_equal(left, right)
        if not equal:
            findings.append({"field": field, "reason": "VALUE_MISMATCH"})
    return {"matched": not findings, "findings": findings,
            "fields_checked": list(fields)}


def write_coverage_report(content_store, trading_session, components,
                          dependency_policy):
    available = {item.component_id for item in components}
    payload = {
        "schema_version": "daily_coverage_report_v1",
        "trading_session": int(trading_session),
        "policy_version": dependency_policy.version,
        "components": [asdict(item) for item in sorted(
            components, key=lambda value: value.component_id)],
        "resolution": dependency_policy.resolve(available),
    }
    path, digest, created = content_store.write_json("coverage", payload)
    return {"path": str(path), "sha256": digest, "created": created,
            "payload": payload}
