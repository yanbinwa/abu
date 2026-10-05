from __future__ import absolute_import

import hashlib
import json
from pathlib import Path


REQUIRED_PREOPEN_INPUTS = (
    "security_master",
    "corporate_actions",
    "security_status",
    "limit_references",
    "receivables",
    "pending_orders",
)


class PreopenSnapshotNotReady(ValueError):
    """A committed preopen snapshot is incomplete or failed quality checks."""


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_preopen_snapshot_row(snapshot, trading_session):
    """Validate the immutable file referenced by a committed catalog row."""
    if (snapshot["snapshot_type"] != "PREOPEN" or
            snapshot["status"] != "COMMITTED" or
            int(snapshot["trading_session"]) != int(trading_session)):
        raise PreopenSnapshotNotReady(
            "committed PREOPEN snapshot for trading session is required")
    path = Path(snapshot["manifest_path"])
    if (not path.is_file() or
            _file_sha256(path) != snapshot["manifest_sha256"]):
        raise PreopenSnapshotNotReady("preopen snapshot manifest is corrupt")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("snapshot_id") != snapshot["snapshot_id"]:
        raise PreopenSnapshotNotReady("preopen snapshot identity mismatch")
    required = tuple(document.get("required_inputs", ()))
    missing = tuple(document.get("missing_inputs", ()))
    if set(required) != set(REQUIRED_PREOPEN_INPUTS):
        raise PreopenSnapshotNotReady("preopen required input contract mismatch")
    if missing:
        raise PreopenSnapshotNotReady(
            "preopen required inputs are missing: {}".format(
                ", ".join(sorted(missing))))
    if document.get("quality_codes"):
        raise PreopenSnapshotNotReady("preopen snapshot failed quality checks")
    return document


class PreopenSnapshotBuilder(object):
    """Build and publish one immutable preopen input bundle per session."""

    def __init__(self, snapshot_catalog):
        self.catalog = snapshot_catalog

    @staticmethod
    def manifest(*, trading_session, decision_cutoff, created_at,
                 security_master_version=None,
                 corporate_action_version=None,
                 security_status_version=None,
                 limit_reference_version=None,
                 receivables_cutoff=None,
                 pending_order_snapshot_id=None,
                 quality_codes=()):
        session = int(trading_session)
        values = {
            "security_master": security_master_version,
            "corporate_actions": corporate_action_version,
            "security_status": security_status_version,
            "limit_references": limit_reference_version,
            "receivables": receivables_cutoff,
            "pending_orders": pending_order_snapshot_id,
        }
        missing = sorted(name for name, value in values.items() if not value)
        codes = set(quality_codes)
        if missing:
            codes.add("PREOPEN_INPUT_MISSING")
        return {
            "schema_version": "preopen_snapshot_v1",
            "stream_id": "market-preopen:{}".format(session),
            "sequence_no": 1,
            "previous_snapshot_id": None,
            "trading_session": session,
            "decision_cutoff": decision_cutoff,
            "created_at": created_at,
            "security_master_version": security_master_version or "MISSING",
            "corporate_action_version": corporate_action_version or "MISSING",
            "security_status_version": security_status_version or "MISSING",
            "limit_reference_version": limit_reference_version or "MISSING",
            "receivables_cutoff": receivables_cutoff or "MISSING",
            "pending_order_snapshot_id": pending_order_snapshot_id or "MISSING",
            "required_inputs": list(REQUIRED_PREOPEN_INPUTS),
            "missing_inputs": missing,
            "quality_codes": sorted(codes),
        }

    def publish(self, **kwargs):
        manifest = self.manifest(**kwargs)
        return self.catalog.publish(
            "PREOPEN", manifest, source_service="preopen-snapshot-builder",
            event_type="PreopenSnapshotCommitted")
