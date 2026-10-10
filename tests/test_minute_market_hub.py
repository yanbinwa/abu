import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from abupy.MarketBu.ABuMinuteBarStore import MinuteBarStore
from abupy.ServiceBu import (
    IncompleteMinuteCollection, MinuteCollector, MinuteSnapshotBuilder,
    MinuteSnapshotConsumer, OperationalStore, SnapshotCatalog,
    WatchlistManager,
)
from tests.test_minute_bar_store import event
from tests.test_intraday_shadow import FakeAdapter, frame


NOW = "2026-10-09T09:31:05+08:00"


class MinuteMarketHubTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.operational = OperationalStore(root / "operational.sqlite3")
        self.catalog = SnapshotCatalog(self.operational, root / "content")
        self.minute_store = MinuteBarStore(root / "minute")
        self.watchlists = WatchlistManager(self.catalog)
        self.builder = MinuteSnapshotBuilder(self.catalog, self.minute_store)

    def tearDown(self):
        self.operational.close()
        self.directory.cleanup()

    def watchlist(self, **changes):
        values = {
            "positions": ("sh600000",),
            "pending_orders": (), "candidates": (),
            "benchmarks": ("sh000300",), "sentinels": (),
        }
        values.update(changes)
        return self.watchlists.publish(20261009, NOW, **values)[0]

    def append(self, symbol, *events):
        records = [replace(item, symbol=symbol) for item in events]
        return self.minute_store.append(records)[0]

    @staticmethod
    def result(source_hash, source="fixture", status="AVAILABLE"):
        return {
            "terminal_status": status, "source": source,
            "partition_manifest_sha256": source_hash,
        }

    def test_watchlist_versions_content_without_changing_minute_stream(self):
        first, created = self.watchlists.publish(
            20261009, NOW, positions=("sh600000",),
            pending_orders=("sz000001",), candidates=("sz300750",),
            benchmarks=("sh000300",), sentinels=("sh600519",))
        self.assertTrue(created)
        reasons = {item["symbol"]: item["reasons"] for item in first["symbols"]}
        self.assertEqual(["POSITION"], reasons["sh600000"])
        self.assertEqual(["PENDING_ORDER"], reasons["sz000001"])
        duplicate, created = self.watchlists.publish(
            20261009, NOW, positions=("sh600000",),
            pending_orders=("sz000001",), candidates=("sz300750",),
            benchmarks=("sh000300",), sentinels=("sh600519",))
        self.assertFalse(created)
        self.assertEqual(first["watchlist_id"], duplicate["watchlist_id"])

        second, created = self.watchlists.publish(
            20261009, "2026-10-09T09:32:05+08:00",
            positions=("sh600000",), pending_orders=(), candidates=("sz300001",),
            benchmarks=("sh000300",), sentinels=("sh600519",))
        self.assertTrue(created)
        self.assertEqual(2, second["watchlist_version"])
        self.assertEqual(first["snapshot_id"], second["previous_snapshot_id"])
        self.assertIn("sh600000", {item["symbol"] for item in second["symbols"]})

    def test_partial_cross_symbol_collection_never_publishes(self):
        symbols = tuple("sz{:06d}".format(index) for index in range(50))
        watchlist, _ = self.watchlists.publish(
            20261009, NOW, sentinels=symbols)
        partial = {
            symbol: {"terminal_status": "PROVIDER_ERROR"}
            for symbol in symbols[:24]}
        with self.assertRaisesRegex(
                IncompleteMinuteCollection, "terminate every"):
            self.builder.publish(
                watchlist, partial, decision_cutoff=NOW,
                collection_started_at="2026-10-09T09:31:00+08:00",
                collection_completed_at=NOW)
        self.assertEqual([], self.catalog.list_committed("MINUTE"))

    def test_revision_is_audited_and_consumers_receive_only_new_business_bars(self):
        watchlist = self.watchlist(positions=("sh600000",), benchmarks=())
        first_partition = self.append("sh600000", event())
        first = self.builder.publish(
            watchlist, {"sh600000": self.result(
                first_partition["manifest_sha256"])},
            decision_cutoff=NOW,
            collection_started_at="2026-10-09T09:31:00+08:00",
            collection_completed_at=NOW)
        self.assertEqual(1, first["manifest"]["sequence_no"])

        second_partition = self.append(
            "sh600000",
            event(available="09:40:00", close=10.15),
            event(end="09:32:00", available="09:32:02", close=10.2))
        second = self.builder.publish(
            watchlist, {"sh600000": self.result(
                second_partition["manifest_sha256"])},
            decision_cutoff="2026-10-09T09:41:00+08:00",
            collection_started_at="2026-10-09T09:40:59+08:00",
            collection_completed_at="2026-10-09T09:41:01+08:00")
        self.assertEqual(2, second["manifest"]["sequence_no"])
        self.assertEqual(first["manifest"]["snapshot_id"],
                         second["manifest"]["previous_snapshot_id"])
        self.assertEqual(first["manifest"]["stream_id"],
                         second["manifest"]["stream_id"])
        self.assertEqual(1, self.operational.connection.execute(
            "SELECT count(*) FROM domain_events "
            "WHERE event_type='LateMinuteRevisionObserved'").fetchone()[0])
        self.assertEqual(1, self.operational.connection.execute(
            "SELECT count(*) FROM audit_findings "
            "WHERE category='LATE_MINUTE_REVISION'").fetchone()[0])
        self.assertEqual(1, second["metrics"]["revised_bar_count"])
        self.assertEqual(0, second["metrics"]["gap_count"])

        consumer = MinuteSnapshotConsumer(
            self.operational, self.catalog, self.minute_store)
        stream = first["manifest"]["stream_id"]
        consumer.register("account-a", stream, NOW)
        consumer.register("account-b", stream, NOW)
        seen_a = []
        seen_b = []

        def handler(target):
            def apply(batch, unused_connection):
                target.append((batch.snapshot_id, {
                    symbol: tuple(item.bar_end for item in events)
                    for symbol, events in batch.events_by_symbol.items()}))
                return {"snapshot_id": batch.snapshot_id}
            return apply

        self.assertEqual("CONSUMED", consumer.consume_next(
            "account-a", handler(seen_a), NOW).status)
        self.assertEqual("CONSUMED", consumer.consume_next(
            "account-b", handler(seen_b), NOW).status)
        self.assertEqual(seen_a[0], seen_b[0])
        self.assertEqual(first["manifest"]["snapshot_id"], seen_a[0][0])
        self.assertEqual("CONSUMED", consumer.consume_next(
            "account-a", handler(seen_a),
            "2026-10-09T09:41:02+08:00").status)
        self.assertEqual(2, len(seen_a))
        delivered = seen_a[1][1]["sh600000"]
        self.assertEqual(1, len(delivered))
        self.assertTrue(delivered[0].endswith("09:32:00+08:00"))

    def test_watchlist_change_does_not_reset_minute_sequence(self):
        first_watchlist = self.watchlist(
            positions=("sh600000",), benchmarks=())
        first_partition = self.append("sh600000", event())
        first = self.builder.publish(
            first_watchlist, {"sh600000": self.result(
                first_partition["manifest_sha256"])}, decision_cutoff=NOW,
            collection_started_at="2026-10-09T09:31:00+08:00",
            collection_completed_at=NOW)

        second_watchlist, _ = self.watchlists.publish(
            20261009, "2026-10-09T09:32:00+08:00",
            positions=("sh600000",), candidates=("sz000001",))
        sh_next = self.append(
            "sh600000", event(end="09:32:00", available="09:32:02"))
        sz_next = self.append(
            "sz000001", event(end="09:32:00", available="09:32:02"))
        second = self.builder.publish(
            second_watchlist, {
                "sh600000": self.result(sh_next["manifest_sha256"]),
                "sz000001": self.result(sz_next["manifest_sha256"]),
            }, decision_cutoff="2026-10-09T09:32:05+08:00",
            collection_started_at="2026-10-09T09:32:00+08:00",
            collection_completed_at="2026-10-09T09:32:04+08:00")
        self.assertNotEqual(first_watchlist["watchlist_id"],
                            second_watchlist["watchlist_id"])
        self.assertEqual(first["manifest"]["stream_id"],
                         second["manifest"]["stream_id"])
        self.assertEqual(2, second["manifest"]["sequence_no"])
        self.assertEqual(first["manifest"]["snapshot_id"],
                         second["manifest"]["previous_snapshot_id"])

    def test_collector_batches_validates_semantics_and_reports_timeout(self):
        calls = []
        adapter = FakeAdapter([event()])
        collector = MinuteCollector(
            adapter, self.minute_store, batch_size=1,
            rate_limiter=lambda: calls.append("permit"))
        result = collector.collect(
            ("sh600000",), 20261009, "2026-10-09 09:31:05")
        self.assertEqual("AVAILABLE", result["sh600000"]["terminal_status"])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, adapter.calls)

        mismatch = MinuteCollector(
            FakeAdapter([replace(event(), symbol="sz000001")]),
            Path(self.directory.name) / "mismatch")
        rejected = mismatch.collect(
            ("sh600000",), 20261009, "2026-10-09 09:31:05")
        self.assertEqual("SCHEMA_ERROR",
                         rejected["sh600000"]["terminal_status"])

        class SlowAdapter(FakeAdapter):
            def minute_bars(self, *args, **kwargs):
                import time
                time.sleep(.01)
                return super().minute_bars(*args, **kwargs)

        timed = MinuteCollector(
            SlowAdapter([event()]), Path(self.directory.name) / "timed",
            request_timeout_seconds=.001).collect(
                ("sh600000",), 20261009, "2026-10-09 09:31:05")
        self.assertEqual("PROVIDER_ERROR", timed["sh600000"]["terminal_status"])
        self.assertEqual("PROVIDER_TIMEOUT", timed["sh600000"]["error"])

        class SwitchingAdapter(FakeAdapter):
            def minute_bars(self, *args, **kwargs):
                self.calls += 1
                source = "provider-a" if self.calls == 1 else "provider-b"
                return frame([replace(event(), source=source)])

        switching = MinuteCollector(
            SwitchingAdapter([]), Path(self.directory.name) / "switching")
        switching.collect(
            ("sh600000",), 20261009, "2026-10-09 09:31:05")
        switched = switching.collect(
            ("sh600000",), 20261009, "2026-10-09 09:31:06")
        self.assertIn("PROVIDER_SWITCHED",
                      switched["sh600000"]["quality_codes"])

    def test_stale_provider_data_is_archived_but_not_counted_as_available(self):
        class StaleAdapter(FakeAdapter):
            def health(self, symbol=None):
                class Health:
                    def to_dict(self):
                        return {"data_fresh": False,
                                "reason_codes": ("STALE_DATA",),
                                "last_warning": "eastmoney_failed: ValueError"}
                return Health()

        watchlist = self.watchlist(positions=("sh600000",), benchmarks=())
        collector = MinuteCollector(StaleAdapter([event()]), self.minute_store)
        results = collector.collect(
            ("sh600000",), 20261009, "2026-10-09 09:35:05")
        self.assertEqual("STALE", results["sh600000"]["terminal_status"])
        published = self.builder.publish(
            watchlist, results, decision_cutoff="2026-10-09T09:35:05+08:00",
            collection_started_at="2026-10-09T09:35:00+08:00",
            collection_completed_at="2026-10-09T09:35:05+08:00")
        self.assertEqual(["sh600000"], published["manifest"]["stale_symbols"])
        self.assertEqual(0.0, published["metrics"]["coverage"])
        self.assertEqual(1, published["metrics"]["selected_bar_count"])
        self.assertIn("PROVIDER_FALLBACK", published["manifest"]["quality_codes"])


if __name__ == "__main__":
    unittest.main()
