import json
import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu import OperationalStore, evaluate_daily_shadow_admission


HASH = "a" * 64


class DailyShadowAdmissionTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = OperationalStore(Path(self.directory.name) / "state.sqlite3")
        self.store.connection.execute(
            "INSERT INTO job_definitions VALUES "
            "('daily.snapshot_shadow', 'v1', 'DAILY_DATA_SHADOW', '{}', 1, 1)")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def add_passed_day(self, session, committed_time="18:55:00"):
        snapshot_id = "daily-{}".format(session)
        timestamp = "{}-{}-{}T{}+08:00".format(
            str(session)[:4], str(session)[4:6], str(session)[6:], committed_time)
        self.store.connection.execute(
            "INSERT INTO market_snapshots "
            "(snapshot_id, snapshot_type, stream_id, sequence_no, trading_session, "
            "decision_cutoff, status, manifest_path, manifest_sha256, created_at, "
            "committed_at) VALUES (?, 'DAILY', ?, 1, ?, ?, 'COMMITTED', ?, ?, ?, ?)",
            (snapshot_id, "market-daily:{}".format(session), session, timestamp,
             snapshot_id + ".json", HASH, timestamp, timestamp))
        self.store.connection.execute(
            "INSERT INTO audit_findings "
            "(finding_id, severity, category, snapshot_id, detail_json, created_at) "
            "VALUES (?, 'INFO', 'DAILY_SHADOW_RECONCILIATION', ?, '{}', ?)",
            ("finding-{}".format(session), snapshot_id, timestamp))
        run_id = "run-{}".format(session)
        self.store.connection.execute(
            "INSERT INTO job_runs "
            "(job_run_id, job_id, job_version, idempotency_key, scheduled_for, status, "
            "output_snapshot_ids_json, created_at, completed_at) "
            "VALUES (?, 'daily.snapshot_shadow', 'v1', ?, ?, 'SUCCEEDED', ?, ?, ?)",
            (run_id, "daily.snapshot_shadow:{}".format(session), timestamp,
             json.dumps([snapshot_id]), timestamp, timestamp))

    def test_requires_five_complete_consecutive_observations(self):
        sessions = [20261009, 20261012, 20261013, 20261014, 20261015]
        for session in sessions:
            self.add_passed_day(session)
        result = evaluate_daily_shadow_admission(
            self.store, sessions, first_eligible_session=20261009,
            asof_session=20261015)
        self.assertTrue(result["passed"])
        self.assertEqual("DAILY_DATA_SHADOW_ACCEPTED", result["status"])

    def test_missing_late_or_failed_day_keeps_gate_pending(self):
        sessions = [20261009, 20261012, 20261013, 20261014, 20261015]
        for session in sessions[:-1]:
            self.add_passed_day(session)
        result = evaluate_daily_shadow_admission(
            self.store, sessions, first_eligible_session=20261009,
            asof_session=20261015)
        self.assertFalse(result["passed"])
        self.assertIn("MISSING_COMMITTED_DAILY_SNAPSHOT",
                      result["observations"][-1]["finding_codes"])

        self.add_passed_day(20261015, committed_time="19:01:00")
        result = evaluate_daily_shadow_admission(
            self.store, sessions, first_eligible_session=20261009,
            asof_session=20261015)
        self.assertFalse(result["passed"])
        self.assertIn("DAILY_SNAPSHOT_AFTER_DEADLINE",
                      result["observations"][-1]["finding_codes"])


if __name__ == "__main__":
    unittest.main()
