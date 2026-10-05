import json
import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu.ABuServiceRuntime import ServiceRuntime, _git_head


class ServiceRuntimeTest(unittest.TestCase):

    def test_frozen_runtime_manifest_supplies_source_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime_manifest.json").write_text(json.dumps({
                "source_commit": "abc123",
            }), encoding="utf-8")
            self.assertEqual("abc123", _git_head(root))

    def _files(self, root, **overrides):
        config = {
            "schema_version": "service_config_v1",
            "service_id": "test-service",
            "timezone": "Asia/Shanghai",
            "runtime_root": str(root),
            "database_path": str(root / "state" / "operational.sqlite3"),
            "lock_path": str(root / "run" / "service.lock"),
            "snapshot_root": str(root / "snapshots"),
            "backup_root": str(root / "backups"),
            "busy_timeout_ms": 5000,
            "research_only": True,
            "broker_connected": False,
            "account_writes_enabled": False,
            "minute_execution_admission": "ACCEPT_DATA_ONLY",
        }
        config.update(overrides)
        jobs = {
            "schema_version": "job_config_v1", "timezone": "Asia/Shanghai",
            "daily_ready_deadline": "19:00:00",
            "jobs": [{
                "job_id": "heartbeat", "job_version": "v1",
                "category": "HEALTH", "critical": False,
                "schedule": {"interval_seconds": 60},
            }],
        }
        config_path = root / "config.json"
        jobs_path = root / "jobs.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
        return config_path, jobs_path

    def test_start_and_stop_are_safe_and_auditable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, jobs = self._files(root)
            service = ServiceRuntime(config, jobs)
            result = service.start()
            self.assertEqual(0, result["recovered_instances"])
            self.assertEqual([], result["schedule"]["registered"])
            self.assertEqual(["heartbeat"], result["schedule"]["deferred"])
            heartbeat = json.loads(service.heartbeat_path.read_text(encoding="utf-8"))
            self.assertFalse(heartbeat["broker_connected"])
            self.assertFalse(heartbeat["account_writes_enabled"])
            self.assertEqual(["heartbeat"], heartbeat["schedule"]["deferred"])
            instance_id = service.instance_id
            service.stop("test")
            connection = __import__("sqlite3").connect(
                str(root / "state" / "operational.sqlite3"))
            try:
                row = connection.execute(
                    "SELECT stopped_at, stop_reason FROM service_instances "
                    "WHERE service_instance_id=?", (instance_id,)).fetchone()
                self.assertEqual("test", row[1])
                self.assertIsNotNone(row[0])
            finally:
                connection.close()

    def test_unsafe_account_write_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, jobs = self._files(root, account_writes_enabled=True)
            with self.assertRaisesRegex(ValueError, "account writes"):
                ServiceRuntime(config, jobs)

    def test_scheduler_registration_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, jobs = self._files(root)
            service = ServiceRuntime(config, jobs)
            called = []
            result = service.start(handlers={"heartbeat": lambda: called.append(True)})
            self.assertEqual(["heartbeat"], result["schedule"]["registered"])
            self.assertEqual([], result["schedule"]["deferred"])
            service.stop("test")
            self.assertEqual([], called)

    def test_signal_requests_graceful_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, jobs = self._files(root)
            service = ServiceRuntime(config, jobs)
            service.request_stop(15)
            self.assertTrue(service.stop_event.is_set())
            self.assertEqual("signal:15", service.requested_stop_reason)

    def test_running_heartbeat_preserves_registered_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, jobs = self._files(root)
            service = ServiceRuntime(config, jobs)
            service.start(handlers={"heartbeat": lambda: None})
            service.write_heartbeat("RUNNING")
            heartbeat = json.loads(service.heartbeat_path.read_text(encoding="utf-8"))
            self.assertEqual(["heartbeat"], heartbeat["schedule"]["registered"])
            self.assertEqual([], heartbeat["schedule"]["deferred"])
            service.stop("test")

    def test_wrapped_job_persists_attempt_and_does_not_repeat_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, jobs = self._files(root)
            service = ServiceRuntime(config, jobs)
            calls = []
            service.start(handlers={"heartbeat": lambda: calls.append(True) or {
                "status": "OK", "output_snapshot_ids": []}})
            job = service.scheduler.scheduler.get_job("heartbeat")
            first = job.func()
            second = job.func()
            self.assertEqual("OK", first["status"])
            self.assertEqual("ALREADY_SUCCEEDED", second["status"])
            self.assertEqual([True], calls)
            self.assertEqual(1, service.store.connection.execute(
                "SELECT count(*) FROM job_run_attempts").fetchone()[0])
            service.stop("test")


if __name__ == "__main__":
    unittest.main()
