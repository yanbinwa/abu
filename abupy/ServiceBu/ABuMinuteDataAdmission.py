from __future__ import absolute_import

import json
from pathlib import Path

from .ABuMinuteMarketHub import minute_snapshot_metrics


def load_minute_admission_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "schema_version", "first_eligible_session",
        "required_effective_sessions", "minimum_snapshots_per_session",
        "minimum_mean_symbol_coverage", "maximum_p99_latency_ms",
        "maximum_gap_count_per_session", "require_no_unresolved_error_findings",
        "admission_state_before_pass",
    }
    if set(payload) != required:
        raise ValueError("minute admission config fields mismatch")
    if payload["schema_version"] != "minute_shadow_admission_v1":
        raise ValueError("minute admission config version mismatch")
    if payload["required_effective_sessions"] < 1 or \
            payload["minimum_snapshots_per_session"] < 1:
        raise ValueError("minute admission counts must be positive")
    return payload


def evaluate_minute_data_admission(operational_store, snapshot_catalog,
                                   expected_sessions, config):
    sessions = [int(item) for item in expected_sessions
                if int(item) >= int(config["first_eligible_session"])]
    sessions = sessions[-int(config["required_effective_sessions"]):]
    observations = []
    for session in sessions:
        rows = snapshot_catalog.list_committed("MINUTE", trading_session=session)
        manifests = [snapshot_catalog.manifest(row["snapshot_id"]) for row in rows]
        metrics = [minute_snapshot_metrics(item) for item in manifests]
        coverages = [item["coverage"] for item in metrics]
        max_p99 = max((item["p99_latency_ms"] for item in metrics), default=0.0)
        gaps = sum(item["gap_count"] for item in metrics)
        severe = operational_store.connection.execute(
            "SELECT count(*) FROM audit_findings WHERE resolved_at IS NULL "
            "AND severity IN ('ERROR', 'CRITICAL') "
            "AND (detail_json LIKE ? OR snapshot_id IN "
            "(SELECT snapshot_id FROM market_snapshots WHERE trading_session=?))",
            ("%{}%".format(session), session)).fetchone()[0]
        mean_coverage = (sum(coverages) / len(coverages) if coverages else 0.0)
        passed = (
            len(rows) >= int(config["minimum_snapshots_per_session"]) and
            mean_coverage >= float(config["minimum_mean_symbol_coverage"]) and
            max_p99 <= float(config["maximum_p99_latency_ms"]) and
            gaps <= int(config["maximum_gap_count_per_session"]) and
            (not config["require_no_unresolved_error_findings"] or severe == 0))
        observations.append({
            "trading_session": session, "snapshot_count": len(rows),
            "mean_symbol_coverage": mean_coverage,
            "maximum_p99_latency_ms": max_p99, "gap_count": gaps,
            "unresolved_error_findings": severe, "passed": passed,
        })
    required = int(config["required_effective_sessions"])
    accepted = len(observations) == required and all(
        item["passed"] for item in observations)
    return {
        "schema_version": "minute_shadow_admission_result_v1",
        "status": ("MINUTE_DATA_ONLY_ACCEPTED" if accepted else "PENDING"),
        "passed": accepted, "required_sessions": required,
        "expected_sessions_seen": sum(
            item["snapshot_count"] > 0 for item in observations),
        "observations": observations,
        "admission_state": config["admission_state_before_pass"],
    }
