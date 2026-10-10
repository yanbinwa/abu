import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from abupy.ServiceBu import MinuteShadowSnapshotJob, OperationalStore
from tests.test_intraday_shadow import FakeAdapter
from tests.test_minute_bar_store import event


ROOT = Path(__file__).resolve().parents[1]


class MinuteShadowSnapshotJobTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = OperationalStore(self.root / "operational.sqlite3")
        self.calendar = self.root / "calendar.json"
        self.calendar.write_text(json.dumps({"dates": [20261009]}), encoding="utf-8")
        self.legacy = self.root / "legacy.json"
        self.legacy.write_text(json.dumps({
            "active": {
                "positions": {"sh600000": {"quantity": 100}},
                "orders": [{"symbol": "sh600000", "status": "WAITING"}],
                "entry_intents": {"one": {"symbol": "sh600000"}},
            },
        }), encoding="utf-8")
        self.config = self.root / "minute.json"
        self.config.write_text(json.dumps({
            "schema_version": "minute_shadow_job_v1",
            "execution_mode": "DATA_ONLY",
            "first_eligible_session": 20261009,
            "minute_store_root": str(self.root / "minute"),
            "trading_calendar_path": str(self.calendar),
            "collection_windows": [
                {"start": "09:31:05", "end": "11:30:30"},
                {"start": "13:01:05", "end": "15:01:30"},
            ],
            "sentinel_symbols": ["sh600000"],
            "benchmark_symbols": ["sh600000"],
            "legacy_state_paths": [str(self.legacy)],
            "batch_size": 20,
            "request_timeout_seconds": 15.0,
            "provider_retries": 1,
            "provider_retry_wait_seconds": 0.0,
            "provider_requests_per_second": 1000.0,
            "provider_policy_version": "fixture-v1",
        }), encoding="utf-8")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    @staticmethod
    def clock(value):
        instant = datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Shanghai"))
        return lambda: instant

    def test_commits_one_data_only_shared_snapshot(self):
        job = MinuteShadowSnapshotJob(
            self.store, self.root / "content", self.config,
            adapter=FakeAdapter([event()]),
            clock=self.clock("2026-10-09T09:31:05+08:00"))
        result = job()
        self.assertEqual("COMMITTED", result["status"])
        self.assertEqual(1, result["metrics"]["symbol_count"])
        self.assertEqual(1.0, result["metrics"]["coverage"])
        manifest = job.catalog.manifest(result["snapshot_id"])
        self.assertTrue(manifest["watchlist_id"].startswith("watchlist-20261009-"))
        watchlist = job.catalog.list_committed("WATCHLIST", 20261009)
        self.assertEqual(1, len(watchlist))
        watchlist_manifest = job.catalog.manifest(watchlist[0]["snapshot_id"])
        self.assertEqual(
            ["POSITION", "PENDING_ORDER", "CANDIDATE", "BENCHMARK", "SENTINEL"],
            watchlist_manifest["symbols"][0]["reasons"])
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM account_events").fetchone()[0])

    def test_calendar_and_collection_window_fail_closed(self):
        before = MinuteShadowSnapshotJob(
            self.store, self.root / "content", self.config,
            adapter=FakeAdapter([event()]),
            clock=self.clock("2026-10-05T09:31:05+08:00"))()
        self.assertEqual("SKIPPED_BEFORE_ELIGIBLE_SESSION", before["status"])

        outside = MinuteShadowSnapshotJob(
            self.store, self.root / "content", self.config,
            adapter=FakeAdapter([event()]),
            clock=self.clock("2026-10-09T12:00:00+08:00"))()
        self.assertEqual("SKIPPED_OUTSIDE_COLLECTION_WINDOW", outside["status"])
        self.assertEqual([], self.store.connection.execute(
            "SELECT * FROM market_snapshots").fetchall())

    def test_rejects_any_execution_mode(self):
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["execution_mode"] = "PAPER"
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "DATA_ONLY"):
            MinuteShadowSnapshotJob(
                self.store, self.root / "content", self.config,
                adapter=FakeAdapter([event()]))

    def test_watchlist_cap_keeps_positions_and_pending_before_candidates(self):
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload["maximum_watchlist_symbols"] = 5
        payload["hard_maximum_watchlist_symbols"] = 5
        payload["benchmark_symbols"] = ["sh600004"]
        payload["sentinel_symbols"] = ["sh600003"]
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        self.legacy.write_text(json.dumps({
            "active": {
                "positions": {"sh600001": {"quantity": 100}},
                "orders": [{"symbol": "sh600002", "status": "WAITING"}],
                "entry_intents": {
                    str(index): {"symbol": "sz{:06d}".format(index + 10)}
                    for index in range(20)},
            },
        }), encoding="utf-8")
        job = MinuteShadowSnapshotJob(
            self.store, self.root / "content", self.config,
            adapter=FakeAdapter([event()]))

        inputs = job._watchlist_inputs()
        admitted = set().union(*map(set, inputs.values()))

        self.assertEqual(5, len(admitted))
        self.assertIn("sh600001", admitted)
        self.assertIn("sh600002", admitted)
        self.assertIn("sh600003", admitted)
        self.assertIn("sh600004", admitted)
        self.assertEqual(1, len(inputs["candidates"]))

    def test_operational_collection_and_admission_start_together(self):
        collection = json.loads((
            ROOT / "configs/service/minute_shadow_v1.json").read_text(
                encoding="utf-8"))
        admission = json.loads((
            ROOT / "configs/service/minute_shadow_admission_v1.json").read_text(
                encoding="utf-8"))
        self.assertEqual(20261008, collection["first_eligible_session"])
        self.assertEqual(collection["first_eligible_session"],
                         admission["first_eligible_session"])


if __name__ == "__main__":
    unittest.main()
