import json
import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu import (
    DomainEventStore, EventDispatcher, OperationalStore, build_domain_event,
    classify_late_event,
)


NOW = "2026-10-09T09:31:05+08:00"
STREAM = "market-minute:20261009:1"


def make_event(sequence, previous=None):
    return build_domain_event(
        "MinuteSnapshotCommitted", STREAM, sequence, {"sequence": sequence},
        previous_event_id=previous, occurred_at=NOW, available_at=NOW,
        source_service="test", trading_session=20261009)


class EventDispatcherTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = OperationalStore(Path(self.directory.name) / "state.sqlite3")
        self.events = DomainEventStore(self.store)
        self.dispatcher = EventDispatcher(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def register(self, effective=1):
        self.events.register_consumer(
            "consumer-a", STREAM, effective, required=True,
            retention_class="PERMANENT_AUDIT", created_at=NOW)

    def test_business_change_and_ack_are_one_transaction(self):
        item = make_event(1)
        self.events.append(item)
        self.register()

        def failing_handler(unused_event, connection):
            connection.execute(
                "INSERT INTO audit_findings "
                "(finding_id, severity, category, detail_json, created_at) "
                "VALUES ('business-write', 'INFO', 'TEST', '{}', ?)", (NOW,))
            raise RuntimeError("fault after business write")

        with self.assertRaisesRegex(RuntimeError, "fault after business write"):
            self.dispatcher.dispatch_next("consumer-a", failing_handler, NOW)
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM audit_findings WHERE finding_id='business-write'"
        ).fetchone()[0])
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM event_consumptions").fetchone()[0])

        calls = []

        def handler(event, connection):
            calls.append(event["event_id"])
            connection.execute(
                "INSERT INTO audit_findings "
                "(finding_id, severity, category, detail_json, created_at) "
                "VALUES ('business-write', 'INFO', 'TEST', '{}', ?)", (NOW,))
            return {"applied": True}

        result = self.dispatcher.dispatch_next("consumer-a", handler, NOW)
        self.assertEqual("CONSUMED", result.status)
        self.assertEqual([item["event_id"]], calls)
        self.assertEqual("IDLE", self.dispatcher.dispatch_next(
            "consumer-a", handler, NOW).status)
        self.assertEqual(1, self.store.connection.execute(
            "SELECT count(*) FROM event_consumptions").fetchone()[0])

        self.store.connection.execute(
            "UPDATE stream_watermarks SET last_consumed_sequence=0 "
            "WHERE consumer_id='consumer-a'")
        repaired = self.dispatcher.dispatch_next("consumer-a", handler, NOW)
        self.assertEqual("ALREADY_CONSUMED", repaired.status)
        self.assertEqual([item["event_id"]], calls)

    def test_missing_sequence_waits_then_blocks_without_advancing(self):
        self.register()
        item = make_event(2)
        self.store.connection.execute(
            "INSERT INTO domain_events "
            "(event_id, stream_id, sequence_no, event_type, schema_version, "
            "occurred_at, available_at, trading_session, source_service, payload_sha256, "
            "payload_json, source_transaction_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["event_id"], STREAM, 2, item["event_type"], item["schema_version"],
             NOW, NOW, 20261009, "test", item["payload_sha256"],
             json.dumps(item["payload"]), item["source_transaction_id"], NOW))
        handler = lambda unused_event, unused_connection: {}
        waiting = self.dispatcher.dispatch_next("consumer-a", handler, NOW)
        self.assertEqual("WAITING_FOR_GAP", waiting.status)
        blocked = self.dispatcher.dispatch_next(
            "consumer-a", handler, NOW, gap_timeout_reached=True)
        self.assertEqual("BLOCKED", blocked.status)
        watermark = self.store.connection.execute(
            "SELECT * FROM stream_watermarks WHERE consumer_id='consumer-a'"
        ).fetchone()
        self.assertEqual(0, watermark["last_consumed_sequence"])

    def test_restart_recovery_drains_persisted_events(self):
        first = make_event(1)
        second = make_event(2, first["event_id"])
        self.events.append(first)
        self.events.append(second)
        self.register()
        applied = []

        def handler(event, unused_connection):
            applied.append(event["sequence_no"])
            return {"sequence": event["sequence_no"]}

        result = self.dispatcher.recover({"consumer-a": handler}, NOW)
        self.assertEqual([1, 2], applied)
        self.assertEqual(2, result["consumer-a"]["processed"])
        self.assertEqual("IDLE", result["consumer-a"]["terminal_status"])
        self.assertTrue(self.events.archive_eligibility(STREAM, 2)["eligible"])

    def test_late_revision_never_rewrites_committed_business_fact(self):
        value = classify_late_event(
            "MinuteBarRevision", business_time="2026-10-09T09:35:00+08:00",
            watermark_time="2026-10-09T09:40:00+08:00",
            committed_business_fact=True)
        self.assertEqual("APPEND_AUDIT_CORRECTION_ONLY", value["action"])
        self.assertEqual("ON_TIME", classify_late_event(
            "MinuteBarRevision", business_time="2026-10-09T09:41:00+08:00",
            watermark_time="2026-10-09T09:40:00+08:00",
            committed_business_fact=False)["classification"])


if __name__ == "__main__":
    unittest.main()
