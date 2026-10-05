import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu import (
    OperationalStore, PreopenSnapshotBuilder, PreopenSnapshotNotReady,
    REQUIRED_PREOPEN_INPUTS, SnapshotCatalog, validate_preopen_snapshot_row,
)


NOW = "2026-10-09T09:20:01+08:00"


class PreopenSnapshotBuilderTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = OperationalStore(root / "operational.sqlite3")
        self.catalog = SnapshotCatalog(self.store, root / "content")
        self.builder = PreopenSnapshotBuilder(self.catalog)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    @staticmethod
    def inputs(**overrides):
        values = {
            "trading_session": 20261009,
            "decision_cutoff": "2026-10-09T09:20:00+08:00",
            "created_at": NOW,
            "security_master_version": "security-v1",
            "corporate_action_version": "actions-v1",
            "security_status_version": "status-v1",
            "limit_reference_version": "limits-v1",
            "receivables_cutoff": "2026-10-09T09:20:00+08:00",
            "pending_order_snapshot_id": "orders-v1",
        }
        values.update(overrides)
        return values

    def test_complete_bundle_publishes_one_ready_snapshot(self):
        snapshot, event, created = self.builder.publish(**self.inputs())
        self.assertTrue(created)
        self.assertEqual("PREOPEN", snapshot["snapshot_type"])
        self.assertEqual("PreopenSnapshotCommitted", event["event_type"])
        manifest = self.catalog.manifest(snapshot["snapshot_id"])
        self.assertEqual(set(REQUIRED_PREOPEN_INPUTS),
                         set(manifest["required_inputs"]))
        self.assertEqual([], manifest["missing_inputs"])
        self.assertEqual([], manifest["quality_codes"])
        self.assertEqual(manifest, validate_preopen_snapshot_row(
            snapshot, 20261009))
        replay, replay_event, replay_created = self.builder.publish(
            **self.inputs())
        self.assertFalse(replay_created)
        self.assertEqual(snapshot["snapshot_id"], replay["snapshot_id"])
        self.assertEqual(event["event_id"], replay_event["event_id"])

    def test_changed_second_bundle_cannot_replace_committed_session_bundle(self):
        self.builder.publish(**self.inputs())
        with self.assertRaisesRegex(ValueError, "sequence is not contiguous"):
            self.builder.publish(**self.inputs(
                security_master_version="security-v2"))

    def test_missing_input_is_published_for_audit_but_not_ready(self):
        snapshot, unused_event, unused_created = self.builder.publish(
            **self.inputs(security_status_version=None))
        manifest = self.catalog.manifest(snapshot["snapshot_id"])
        self.assertEqual(["security_status"], manifest["missing_inputs"])
        self.assertEqual(["PREOPEN_INPUT_MISSING"], manifest["quality_codes"])
        with self.assertRaisesRegex(
                PreopenSnapshotNotReady, "security_status"):
            validate_preopen_snapshot_row(snapshot, 20261009)

    def test_nonempty_quality_code_fails_readiness(self):
        snapshot, unused_event, unused_created = self.builder.publish(
            **self.inputs(quality_codes=("SECURITY_STATUS_STALE",)))
        with self.assertRaisesRegex(
                PreopenSnapshotNotReady, "quality checks"):
            validate_preopen_snapshot_row(snapshot, 20261009)

    def test_manifest_corruption_fails_readiness(self):
        snapshot, unused_event, unused_created = self.builder.publish(
            **self.inputs())
        Path(snapshot["manifest_path"]).write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(PreopenSnapshotNotReady, "corrupt"):
            validate_preopen_snapshot_row(snapshot, 20261009)


if __name__ == "__main__":
    unittest.main()
