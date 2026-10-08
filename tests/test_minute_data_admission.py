import tempfile
import unittest
import json
from pathlib import Path

from abupy.ServiceBu import (
    OperationalStore, SnapshotCatalog, evaluate_minute_data_admission,
)
from scripts.audit_minute_shadow_admission import trading_sessions


class MinuteDataAdmissionTest(unittest.TestCase):

    def test_audit_sessions_come_from_frozen_trading_calendar(self):
        with tempfile.TemporaryDirectory() as directory:
            calendar = Path(directory) / "calendar.json"
            calendar.write_text(json.dumps({
                "dates": [20261002, 20261008, 20261009, 20261008],
            }), encoding="utf-8")
            self.assertEqual(
                [20261002, 20261008],
                trading_sessions(calendar, 20261008))

    @staticmethod
    def publish(catalog, session):
        stamp = "{}-{}-{}T09:31:05+08:00".format(
            str(session)[:4], str(session)[4:6], str(session)[6:])
        bar_end = stamp.replace("09:31:05", "09:31:00")
        manifest = {
            "schema_version": "minute_snapshot_v1",
            "stream_id": "market-minute:{}:1".format(session),
            "sequence_no": 1, "previous_snapshot_id": None,
            "trading_session": session, "interval_minutes": 1,
            "watchlist_id": "watchlist-{}".format(session),
            "watchlist_version": 1, "decision_cutoff": stamp,
            "collection_started_at": stamp,
            "collection_completed_at": stamp, "created_at": stamp,
            "provider_policy_version": "fixture",
            "bar_selection_policy_version":
                "available_at_cutoff_latest_revision_v1",
            "symbols": {"sh600000": {
                "partition_manifest_sha256": "a" * 64,
                "selected_bar_set_sha256": "b" * 64,
                "terminal_status": "AVAILABLE", "latest_available_at": stamp,
                "selected_bars": [{
                    "business_bar_key": "sh600000:1:{}".format(bar_end),
                    "revision": 1, "event_id": "bar-{}".format(session),
                    "available_at": stamp,
                }],
            }},
            "missing_symbols": [], "stale_symbols": [], "quality_codes": [],
        }
        catalog.publish(
            "MINUTE", manifest, source_service="test",
            event_type="MinuteSnapshotCommitted")

    def test_requires_every_expected_session_and_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OperationalStore(root / "operational.sqlite3")
            try:
                catalog = SnapshotCatalog(store, root / "content")
                config = {
                    "first_eligible_session": 20261009,
                    "required_effective_sessions": 2,
                    "minimum_snapshots_per_session": 1,
                    "minimum_mean_symbol_coverage": .9,
                    "maximum_p99_latency_ms": 15000,
                    "maximum_gap_count_per_session": 0,
                    "require_no_unresolved_error_findings": True,
                    "admission_state_before_pass": "ACCEPT_DATA_ONLY",
                }
                pending = evaluate_minute_data_admission(
                    store, catalog, (20261009, 20261012), config)
                self.assertFalse(pending["passed"])
                self.assertEqual("ACCEPT_DATA_ONLY", pending["admission_state"])
                self.assertEqual(0, pending["expected_sessions_seen"])
                self.publish(catalog, 20261009)
                self.publish(catalog, 20261012)
                accepted = evaluate_minute_data_admission(
                    store, catalog, (20261009, 20261012), config)
                self.assertTrue(accepted["passed"])
                self.assertEqual("MINUTE_DATA_ONLY_ACCEPTED", accepted["status"])
                self.publish(catalog, 20261013)
                rolling = evaluate_minute_data_admission(
                    store, catalog, (20261009, 20261012, 20261013), config)
                self.assertEqual(
                    [20261012, 20261013],
                    [item["trading_session"] for item in rolling["observations"]])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
