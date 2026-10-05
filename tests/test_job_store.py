import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu.ABuJobStore import JobStore
from abupy.ServiceBu.ABuOperationalStore import OperationalStore


class JobStoreTest(unittest.TestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = OperationalStore(Path(self.temporary.name) / "state.sqlite3")
        self.store.record_service_start(
            "service-1", "local", 1, "start", "head", "a" * 64)
        self.jobs = JobStore(self.store)
        self.definition = {
            "job_id": "daily.collect", "job_version": "v1",
            "category": "DAILY_DATA", "critical": True,
            "schedule": {"hour": 18, "minute": 0},
        }
        self.jobs.register_definition(self.definition)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_run_idempotency_and_attempt_history(self):
        first, created = self.jobs.ensure_run(
            "daily.collect", "v1", "daily.collect:20261009",
            "2026-10-09T18:00:00+08:00", "now")
        second, created_again = self.jobs.ensure_run(
            "daily.collect", "v1", "daily.collect:20261009",
            "2026-10-09T18:00:00+08:00", "later")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["job_run_id"], second["job_run_id"])
        one = self.jobs.start_attempt(first["job_run_id"], "service-1", "start-1")
        self.jobs.finish_attempt(one["attempt_id"], "RETRYABLE_FAILED", "end-1",
                                 error_code="NETWORK")
        two = self.jobs.start_attempt(first["job_run_id"], "service-1", "start-2")
        self.jobs.finish_attempt(two["attempt_id"], "SUCCEEDED", "end-2")
        self.assertEqual(1, one["attempt_no"])
        self.assertEqual(2, two["attempt_no"])
        attempts = self.store.connection.execute(
            "SELECT status FROM job_run_attempts ORDER BY attempt_no").fetchall()
        self.assertEqual(["RETRYABLE_FAILED", "SUCCEEDED"],
                         [item["status"] for item in attempts])

    def test_startup_recovers_running_attempt_without_losing_history(self):
        run, _ = self.jobs.ensure_run(
            "daily.collect", "v1", "daily.collect:20261009",
            "scheduled", "created")
        self.jobs.start_attempt(run["job_run_id"], "service-1", "started")
        self.assertEqual(1, self.jobs.recover_running_attempts("recovered"))
        attempt = self.store.connection.execute(
            "SELECT status, error_code FROM job_run_attempts").fetchone()
        job_run = self.store.connection.execute(
            "SELECT status FROM job_runs").fetchone()
        self.assertEqual(("INTERRUPTED", "SERVICE_RESTART"), tuple(attempt))
        self.assertEqual("RETRYABLE_FAILED", job_run["status"])

    def test_definition_drift_is_rejected(self):
        changed = dict(self.definition)
        changed["schedule"] = {"hour": 19, "minute": 0}
        with self.assertRaisesRegex(ValueError, "changed"):
            self.jobs.register_definition(changed)


if __name__ == "__main__":
    unittest.main()
