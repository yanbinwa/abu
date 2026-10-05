import json
import tempfile
import unittest
from pathlib import Path

from jsonschema.exceptions import ValidationError

from abupy.ServiceBu import OperationalStore, SnapshotCatalog


NOW = "2026-10-09T18:05:00+08:00"


def daily_manifest(sequence=1, previous=None):
    return {
        "schema_version": "daily_snapshot_v1",
        "stream_id": "market-daily:20261009",
        "sequence_no": sequence,
        "previous_snapshot_id": previous,
        "trading_session": 20261009,
        "decision_cutoff": "2026-10-09T18:00:00+08:00",
        "created_at": NOW,
        "universe_version": "universe-v1",
        "calendar_version": "calendar-v1",
        "security_master_version": "security-v1",
        "raw_price_version": "raw-v1",
        "adjusted_price_version": "adjusted-v1",
        "industry_version": "industry-v1",
        "valuation_version": "valuation-v1",
        "fundamental_pit_version": "fundamental-v1",
        "corporate_action_version": "actions-v1",
        "limit_reference_version": "limits-v1",
        "required_components": ["raw_price", "adjusted_price", "universe"],
        "missing_components": [],
        "quality_codes": [],
    }


class SnapshotCatalogTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = OperationalStore(root / "state.sqlite3")
        self.catalog = SnapshotCatalog(self.store, root / "content")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_manifest_file_and_database_event_publish_atomically(self):
        snapshot, event, created = self.catalog.publish(
            "DAILY", daily_manifest(), source_service="test")
        self.assertTrue(created)
        self.assertEqual(snapshot["snapshot_id"], event["snapshot_id"])
        self.assertEqual("COMMITTED", snapshot["status"])
        self.assertTrue(Path(snapshot["manifest_path"]).is_file())
        self.assertEqual(snapshot, self.catalog.get(snapshot["snapshot_id"]))

        again, again_event, created = self.catalog.publish(
            "DAILY", daily_manifest(), source_service="test")
        self.assertFalse(created)
        self.assertEqual(snapshot["snapshot_id"], again["snapshot_id"])
        self.assertEqual(event["event_id"], again_event["event_id"])
        self.assertEqual(1, len(self.catalog.list_committed("DAILY", 20261009)))

    def test_file_before_transaction_failure_leaves_only_auditable_orphan(self):
        with self.assertRaisesRegex(RuntimeError, "fault injected"):
            self.catalog.publish(
                "DAILY", daily_manifest(), source_service="test",
                fail_after_file=True)
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM market_snapshots").fetchone()[0])
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM domain_events").fetchone()[0])
        audit = self.catalog.audit(NOW)
        self.assertEqual(1, len(audit["orphan_paths"]))

    def test_event_failure_rolls_back_snapshot_row(self):
        original = self.catalog.events.append

        def fail_event(unused_event, connection=None):
            raise RuntimeError("event append failed")

        self.catalog.events.append = fail_event
        try:
            with self.assertRaisesRegex(RuntimeError, "event append failed"):
                self.catalog.publish("DAILY", daily_manifest(), source_service="test")
        finally:
            self.catalog.events.append = original
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM market_snapshots").fetchone()[0])
        self.assertEqual(1, len(self.catalog.audit(NOW)["orphan_paths"]))

    def test_schema_and_stream_predecessor_are_enforced(self):
        invalid = daily_manifest()
        invalid.pop("raw_price_version")
        with self.assertRaises(ValidationError):
            self.catalog.publish("DAILY", invalid, source_service="test")
        first, unused_event, unused_created = self.catalog.publish(
            "DAILY", daily_manifest(), source_service="test")
        with self.assertRaisesRegex(ValueError, "previous_snapshot_id"):
            self.catalog.publish(
                "DAILY", daily_manifest(2, previous="wrong"), source_service="test")
        second_manifest = daily_manifest(2, previous=first["snapshot_id"])
        second_manifest["created_at"] = "2026-10-09T18:06:00+08:00"
        second, unused_event, unused_created = self.catalog.publish(
            "DAILY", second_manifest, source_service="test")
        self.assertEqual(first["snapshot_id"], second["previous_snapshot_id"])

    def test_missing_or_changed_manifest_marks_snapshot_corrupt(self):
        snapshot, unused_event, unused_created = self.catalog.publish(
            "DAILY", daily_manifest(), source_service="test")
        path = Path(snapshot["manifest_path"])
        document = json.loads(path.read_text(encoding="utf-8"))
        document["quality_codes"] = ["tampered"]
        path.write_text(json.dumps(document), encoding="utf-8")
        audit = self.catalog.audit(NOW)
        self.assertEqual([snapshot["snapshot_id"]], audit["corrupt_snapshot_ids"])
        row = self.store.connection.execute(
            "SELECT status FROM market_snapshots WHERE snapshot_id=?",
            (snapshot["snapshot_id"],)).fetchone()
        self.assertEqual("CORRUPT", row["status"])


if __name__ == "__main__":
    unittest.main()
