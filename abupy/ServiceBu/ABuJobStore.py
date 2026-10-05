from __future__ import absolute_import

import json
import uuid


class JobStore(object):

    def __init__(self, operational_store):
        self.store = operational_store

    def register_definition(self, job):
        schedule = job.get("schedule", {})
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT job_version, category, schedule_json, critical "
                "FROM job_definitions WHERE job_id=?", (job["job_id"],)).fetchone()
            values = (
                job["job_version"], job["category"],
                json.dumps(schedule, sort_keys=True, separators=(",", ":")),
                int(job.get("critical", False)),
            )
            if existing:
                actual = (existing["job_version"], existing["category"],
                          existing["schedule_json"], existing["critical"])
                if actual != values:
                    raise ValueError("job definition changed without a new job_id/version")
                return False
            connection.execute(
                "INSERT INTO job_definitions "
                "(job_id, job_version, category, schedule_json, enabled, critical) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (job["job_id"],) + values)
            return True

    def ensure_run(self, job_id, job_version, idempotency_key, scheduled_for,
                   created_at, dependency_snapshot_ids=()):
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM job_runs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                return dict(existing), False
            run_id = "run-{}".format(uuid.uuid4().hex)
            connection.execute(
                "INSERT INTO job_runs "
                "(job_run_id, job_id, job_version, idempotency_key, scheduled_for, "
                "status, dependency_snapshot_ids_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'SCHEDULED', ?, ?)",
                (run_id, job_id, job_version, idempotency_key, scheduled_for,
                 json.dumps(list(dependency_snapshot_ids), sort_keys=True), created_at))
            return dict(connection.execute(
                "SELECT * FROM job_runs WHERE job_run_id=?", (run_id,)).fetchone()), True

    def start_attempt(self, job_run_id, service_instance_id, started_at):
        with self.store.transaction() as connection:
            run = connection.execute(
                "SELECT status FROM job_runs WHERE job_run_id=?", (job_run_id,)
            ).fetchone()
            if run is None:
                raise KeyError("unknown job run: {}".format(job_run_id))
            if run["status"] == "SUCCEEDED":
                raise ValueError("successful job run cannot be attempted again")
            attempt_no = connection.execute(
                "SELECT coalesce(max(attempt_no), 0) + 1 FROM job_run_attempts "
                "WHERE job_run_id=?", (job_run_id,)).fetchone()[0]
            attempt_id = "attempt-{}".format(uuid.uuid4().hex)
            connection.execute(
                "INSERT INTO job_run_attempts "
                "(attempt_id, job_run_id, attempt_no, service_instance_id, started_at, status) "
                "VALUES (?, ?, ?, ?, ?, 'RUNNING')",
                (attempt_id, job_run_id, attempt_no, service_instance_id, started_at))
            connection.execute(
                "UPDATE job_runs SET status='RUNNING' WHERE job_run_id=?", (job_run_id,))
            return {"attempt_id": attempt_id, "attempt_no": attempt_no}

    def finish_attempt(self, attempt_id, status, finished_at, *, error_code=None,
                       error_detail=None, output_snapshot_ids=()):
        allowed = {"SUCCEEDED", "RETRYABLE_FAILED", "TERMINAL_FAILED", "INTERRUPTED"}
        if status not in allowed:
            raise ValueError("invalid terminal attempt status: {}".format(status))
        with self.store.transaction() as connection:
            attempt = connection.execute(
                "SELECT job_run_id, status FROM job_run_attempts WHERE attempt_id=?",
                (attempt_id,)).fetchone()
            if attempt is None:
                raise KeyError("unknown attempt: {}".format(attempt_id))
            if attempt["status"] != "RUNNING":
                raise ValueError("attempt is already terminal")
            connection.execute(
                "UPDATE job_run_attempts SET finished_at=?, status=?, error_code=?, "
                "error_detail=? WHERE attempt_id=?",
                (finished_at, status, error_code, error_detail, attempt_id))
            run_status = status if status != "INTERRUPTED" else "RETRYABLE_FAILED"
            successful_attempt = attempt_id if status == "SUCCEEDED" else None
            completed_at = finished_at if status in {"SUCCEEDED", "TERMINAL_FAILED"} else None
            connection.execute(
                "UPDATE job_runs SET status=?, output_snapshot_ids_json=?, "
                "successful_attempt_id=?, completed_at=? WHERE job_run_id=?",
                (run_status, json.dumps(list(output_snapshot_ids), sort_keys=True),
                 successful_attempt, completed_at, attempt["job_run_id"]))

    def recover_running_attempts(self, recovered_at):
        with self.store.transaction() as connection:
            rows = list(connection.execute(
                "SELECT attempt_id, job_run_id FROM job_run_attempts WHERE status='RUNNING'"))
            for row in rows:
                connection.execute(
                    "UPDATE job_run_attempts SET status='INTERRUPTED', finished_at=?, "
                    "error_code='SERVICE_RESTART', error_detail='recovered on startup' "
                    "WHERE attempt_id=?", (recovered_at, row["attempt_id"]))
                connection.execute(
                    "UPDATE job_runs SET status='RETRYABLE_FAILED' WHERE job_run_id=?",
                    (row["job_run_id"],))
            return len(rows)
