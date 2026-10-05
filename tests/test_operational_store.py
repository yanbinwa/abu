import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu.ABuOperationalStore import OperationalStore


class OperationalStoreTest(unittest.TestCase):

    def test_initialization_is_idempotent_and_enables_integrity_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first = OperationalStore(path)
            digest = first.schema_sha256
            self.assertEqual("wal", first.connection.execute(
                "PRAGMA journal_mode").fetchone()[0].lower())
            self.assertEqual(1, first.connection.execute(
                "PRAGMA foreign_keys").fetchone()[0])
            first.close()
            second = OperationalStore(path)
            self.assertEqual(digest, second.schema_sha256)
            self.assertEqual({"integrity_check": "ok", "foreign_key_errors": 0},
                             second.integrity_check())
            second.close()

    def test_transaction_rolls_back_all_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OperationalStore(Path(directory) / "state.sqlite3")
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with store.transaction() as connection:
                    connection.execute(
                        "INSERT INTO service_instances "
                        "(service_instance_id, host_name, process_id, started_at, "
                        "repository_commit, config_sha256) VALUES (?, ?, ?, ?, ?, ?)",
                        ("service-1", "local", 1, "now", "head", "a" * 64))
                    raise RuntimeError("boom")
            count = store.connection.execute(
                "SELECT count(*) FROM service_instances").fetchone()[0]
            self.assertEqual(0, count)
            store.close()

    def test_online_backup_and_empty_path_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OperationalStore(root / "state.sqlite3")
            store.record_service_start(
                "service-1", "local", 1, "start", "head", "a" * 64)
            immutable = root / "snapshot.json"
            immutable.write_text("{}\n", encoding="utf-8")
            payload = store.backup(
                root / "backup.sqlite3", root / "backup.json",
                service_instance_id="service-1", immutable_files=[immutable],
                created_at="2026-10-05T00:00:00+08:00")
            store.close()
            self.assertEqual(1, payload["database_schema_version"])
            restored = OperationalStore.restore(
                root / "backup.sqlite3", root / "backup.json", root / "restored.sqlite3")
            connection = sqlite3.connect(str(restored))
            try:
                self.assertEqual(1, connection.execute(
                    "SELECT count(*) FROM service_instances").fetchone()[0])
            finally:
                connection.close()
            with self.assertRaises(FileExistsError):
                OperationalStore.restore(
                    root / "backup.sqlite3", root / "backup.json", restored)

    def test_restore_rejects_tampered_manifest_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OperationalStore(root / "state.sqlite3")
            immutable = root / "snapshot.json"
            immutable.write_text("{}\n", encoding="utf-8")
            store.backup(
                root / "backup.sqlite3", root / "backup.json",
                service_instance_id="service-1", immutable_files=[immutable],
                created_at="2026-10-05T00:00:00+08:00")
            store.close()
            immutable.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "dependency hash mismatch"):
                OperationalStore.restore(
                    root / "backup.sqlite3", root / "backup.json", root / "restored.sqlite3")


if __name__ == "__main__":
    unittest.main()
