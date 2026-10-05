import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu import OperationalStore


class OperationalSchemaV2Test(unittest.TestCase):

    def test_v1_remains_default_and_v2_requires_explicit_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first = OperationalStore(path)
            self.assertEqual(1, first.connection.execute(
                "SELECT max(version) FROM schema_migrations").fetchone()[0])
            self.assertIsNone(first.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='account_receivables'").fetchone())
            first.close()

            upgraded = OperationalStore(path, target_schema_version=2)
            self.assertEqual(2, upgraded.connection.execute(
                "SELECT max(version) FROM schema_migrations").fetchone()[0])
            for table in ("account_receivables", "sell_reservations"):
                self.assertIsNotNone(upgraded.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone())
            upgraded.close()

            with self.assertRaisesRegex(ValueError, "newer than requested"):
                OperationalStore(path)

    def test_v2_reopen_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first = OperationalStore(path, target_schema_version=2)
            digest = first.schema_sha256
            first.close()
            second = OperationalStore(path, target_schema_version=2)
            self.assertEqual(digest, second.schema_sha256)
            self.assertEqual(2, second.connection.execute(
                "SELECT count(*) FROM schema_migrations").fetchone()[0])
            second.close()

    def test_v3_adds_daily_close_projection_without_rewriting_v2(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            v2 = OperationalStore(path, target_schema_version=2)
            v2_hash = v2.connection.execute(
                "SELECT sha256 FROM schema_migrations WHERE version=2"
            ).fetchone()[0]
            v2.close()
            v3 = OperationalStore(path, target_schema_version=3)
            self.assertEqual(v2_hash, v3.connection.execute(
                "SELECT sha256 FROM schema_migrations WHERE version=2"
            ).fetchone()[0])
            self.assertEqual(3, v3.connection.execute(
                "SELECT max(version) FROM schema_migrations").fetchone()[0])
            self.assertIsNotNone(v3.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='account_daily_closes'").fetchone())
            v3.close()


if __name__ == "__main__":
    unittest.main()
