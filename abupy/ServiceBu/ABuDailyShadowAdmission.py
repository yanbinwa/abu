from __future__ import absolute_import

import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


def benchmark_sessions(pattern):
    pattern = Path(pattern)
    matches = sorted(pattern.parent.glob(pattern.name))
    if not matches:
        raise FileNotFoundError("benchmark source is missing")
    sessions = set()
    for path in matches:
        with path.open("r", encoding="utf-8", newline="") as source:
            for row in csv.DictReader(source):
                value = row.get("date")
                if value:
                    sessions.add(int(float(value)))
    return sorted(sessions)


def _deadline(session, deadline):
    day = datetime.strptime(str(int(session)), "%Y%m%d").date()
    parsed = datetime.strptime(deadline, "%H:%M:%S").time()
    return datetime.combine(day, parsed, tzinfo=SHANGHAI)


def evaluate_daily_shadow_admission(operational_store, expected_sessions, *,
                                    first_eligible_session, asof_session,
                                    required_days=5, deadline="19:00:00"):
    eligible = [int(item) for item in sorted(set(expected_sessions))
                if int(first_eligible_session) <= int(item) <= int(asof_session)]
    target = eligible[-int(required_days):]
    observations = []
    for session in target:
        snapshot = operational_store.connection.execute(
            "SELECT * FROM market_snapshots WHERE snapshot_type='DAILY' "
            "AND trading_session=? AND status='COMMITTED' "
            "ORDER BY sequence_no DESC LIMIT 1", (session,)).fetchone()
        finding_codes = []
        if snapshot is None:
            finding_codes.append("MISSING_COMMITTED_DAILY_SNAPSHOT")
            snapshot_id = None
        else:
            snapshot_id = snapshot["snapshot_id"]
            committed_at = datetime.fromisoformat(snapshot["committed_at"])
            if committed_at > _deadline(session, deadline):
                finding_codes.append("DAILY_SNAPSHOT_AFTER_DEADLINE")
            reconciliation = operational_store.connection.execute(
                "SELECT finding_id FROM audit_findings "
                "WHERE category='DAILY_SHADOW_RECONCILIATION' "
                "AND snapshot_id=? AND resolved_at IS NULL", (snapshot_id,)).fetchone()
            if reconciliation is None:
                finding_codes.append("MISSING_DAILY_RECONCILIATION")
        run = operational_store.connection.execute(
            "SELECT * FROM job_runs WHERE idempotency_key=?",
            ("daily.snapshot_shadow:{}".format(session),)).fetchone()
        if run is None or run["status"] != "SUCCEEDED":
            finding_codes.append("DAILY_SHADOW_JOB_NOT_SUCCEEDED")
        elif snapshot_id is not None:
            outputs = json.loads(run["output_snapshot_ids_json"])
            if snapshot_id not in outputs:
                finding_codes.append("JOB_OUTPUT_SNAPSHOT_MISMATCH")
        unresolved = operational_store.connection.execute(
            "SELECT count(*) FROM audit_findings WHERE severity IN ('ERROR', 'CRITICAL') "
            "AND resolved_at IS NULL AND (snapshot_id=? OR detail_json LIKE ?)",
            (snapshot_id, "%{}%".format(session))).fetchone()[0]
        if unresolved:
            finding_codes.append("UNRESOLVED_SEVERE_FINDING")
        observations.append({
            "trading_session": session,
            "snapshot_id": snapshot_id,
            "passed": not finding_codes,
            "finding_codes": finding_codes,
        })
    enough_sessions = len(target) == int(required_days)
    passed = enough_sessions and all(item["passed"] for item in observations)
    return {
        "status": "DAILY_DATA_SHADOW_ACCEPTED" if passed else "PENDING",
        "passed": passed,
        "required_days": int(required_days),
        "first_eligible_session": int(first_eligible_session),
        "asof_session": int(asof_session),
        "expected_sessions_seen": len(eligible),
        "target_sessions": target,
        "observations": observations,
    }
