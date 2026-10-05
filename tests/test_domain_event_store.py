import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu import (
    DomainEventStore, EventCollisionError, OperationalStore,
    StreamSequenceError, build_domain_event,
)


NOW = "2026-10-09T09:31:05+08:00"


def event(sequence, previous=None, payload=None, stream="market-minute:20261009:1"):
    return build_domain_event(
        "MinuteSnapshotCommitted", stream, sequence,
        payload or {"sequence": sequence}, previous_event_id=previous,
        occurred_at=NOW, available_at=NOW, source_service="test",
        trading_session=20261009)


class DomainEventStoreTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = OperationalStore(Path(self.directory.name) / "state.sqlite3")
        self.events = DomainEventStore(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_append_is_deterministic_idempotent_and_strictly_ordered(self):
        first = event(1)
        stored, created = self.events.append(first)
        self.assertTrue(created)
        duplicate, created = self.events.append(first)
        self.assertFalse(created)
        self.assertEqual(stored["event_id"], duplicate["event_id"])

        with self.assertRaisesRegex(StreamSequenceError, "expected sequence 2"):
            self.events.append(event(3, previous=first["event_id"]))
        with self.assertRaisesRegex(StreamSequenceError, "previous_event_id"):
            self.events.append(event(2, previous="wrong"))
        second = event(2, previous=first["event_id"])
        self.events.append(second)
        self.assertEqual(2, self.store.connection.execute(
            "SELECT count(*) FROM domain_events").fetchone()[0])

    def test_same_id_with_different_payload_fails_closed(self):
        first = event(1)
        self.events.append(first)
        self.events.register_consumer(
            "consumer-a", first["stream_id"], 1, required=True,
            retention_class="PERMANENT_AUDIT", created_at=NOW)
        collision = event(1, payload={"sequence": 999})
        collision["event_id"] = first["event_id"]
        with self.assertRaisesRegex(EventCollisionError, "different immutable content"):
            self.events.append(collision)
        self.assertEqual("BLOCKED", self.store.connection.execute(
            "SELECT state FROM stream_watermarks WHERE consumer_id='consumer-a'"
        ).fetchone()["state"])
        self.assertEqual(1, self.store.connection.execute(
            "SELECT count(*) FROM audit_findings "
            "WHERE category='DOMAIN_EVENT_COLLISION' AND severity='CRITICAL'"
        ).fetchone()[0])

    def test_consumer_registration_and_archive_boundary(self):
        first = event(1)
        second = event(2, previous=first["event_id"])
        self.events.append(first)
        self.events.append(second)
        _, created = self.events.register_consumer(
            "consumer-a", first["stream_id"], 2, required=True,
            retention_class="PERMANENT_AUDIT", created_at=NOW)
        self.assertTrue(created)
        pending = self.events.unconsumed("consumer-a")
        self.assertEqual([2], [item["sequence_no"] for item in pending])
        eligibility = self.events.archive_eligibility(first["stream_id"], 2)
        self.assertFalse(eligibility["eligible"])
        self.assertFalse(eligibility["automatic_delete"])
        with self.assertRaisesRegex(ValueError, "required consumers"):
            self.events.record_archive_review(
                first["stream_id"], 2, reviewed_by="operator", reviewed_at=NOW,
                approved=True)
        review = self.events.record_archive_review(
            first["stream_id"], 2, reviewed_by="operator", reviewed_at=NOW,
            approved=False)
        self.assertFalse(review["automatic_delete"])

        with self.assertRaisesRegex(ValueError, "registration is immutable"):
            self.events.register_consumer(
                "consumer-a", first["stream_id"], 1, required=True,
                retention_class="PERMANENT_AUDIT", created_at=NOW)

    def test_retirement_requires_caught_up_boundary_or_explicit_audit(self):
        stream = "market-daily:20261009"
        self.events.append(event(1, stream=stream))
        self.events.register_consumer(
            "consumer-a", stream, 1, required=True,
            retention_class="ARCHIVE_AFTER_BACKUP", created_at=NOW)
        with self.assertRaisesRegex(ValueError, "retirement boundary"):
            self.events.retire_consumer("consumer-a", NOW, 1)
        boundary = self.events.retire_consumer(
            "consumer-a", NOW, 1, approved_gap=True)
        self.assertEqual(1, boundary)
        self.assertEqual(1, self.store.connection.execute(
            "SELECT count(*) FROM audit_findings "
            "WHERE category='CONSUMER_RETIREMENT_GAP'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
