from __future__ import absolute_import

import json

from .ABuContentStore import sha256_json


class DispatchResult(object):

    def __init__(self, status, **details):
        self.status = status
        self.details = details

    def as_dict(self):
        value = {"status": self.status}
        value.update(self.details)
        return value


class EventDispatcher(object):
    """Strict in-order dispatcher for one registered consumer and stream."""

    def __init__(self, operational_store):
        self.store = operational_store

    def dispatch_next(self, consumer_id, handler, now, *, gap_timeout_reached=False):
        consumer = self.store.connection.execute(
            "SELECT * FROM event_consumers WHERE consumer_id=?", (consumer_id,)
        ).fetchone()
        if consumer is None:
            raise KeyError("unknown event consumer: {}".format(consumer_id))
        stream_id = consumer["effective_from_stream"]
        watermark = self.store.connection.execute(
            "SELECT * FROM stream_watermarks WHERE consumer_id=? AND stream_id=?",
            (consumer_id, stream_id)).fetchone()
        if consumer["retired_at"] is not None or watermark["state"] == "RETIRED":
            return DispatchResult("RETIRED")

        expected = max(consumer["effective_from_sequence"],
                       watermark["last_consumed_sequence"] + 1)
        event = self.store.connection.execute(
            "SELECT * FROM domain_events WHERE stream_id=? AND sequence_no>=? "
            "ORDER BY sequence_no LIMIT 1", (stream_id, expected)).fetchone()
        if event is None:
            return DispatchResult("IDLE", expected_sequence=expected)
        if event["sequence_no"] != expected:
            state = "BLOCKED" if gap_timeout_reached else "WAITING_FOR_GAP"
            severity = "ERROR" if gap_timeout_reached else "WARNING"
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE stream_watermarks SET state=?, updated_at=? "
                    "WHERE consumer_id=? AND stream_id=?",
                    (state, now, consumer_id, stream_id))
                connection.execute(
                    "INSERT OR IGNORE INTO audit_findings "
                    "(finding_id, severity, category, event_id, detail_json, created_at) "
                    "VALUES (?, ?, 'EVENT_STREAM_GAP', ?, ?, ?)",
                    ("gap-{}-{}-{}".format(consumer_id, stream_id, expected), severity,
                     event["event_id"], json.dumps({
                         "consumer_id": consumer_id,
                         "stream_id": stream_id,
                         "expected_sequence": expected,
                         "observed_sequence": event["sequence_no"],
                     }, sort_keys=True), now))
            return DispatchResult(state, expected_sequence=expected,
                                  observed_sequence=event["sequence_no"])

        if event["previous_event_id"] is not None:
            previous = self.store.connection.execute(
                "SELECT event_id FROM domain_events WHERE stream_id=? AND sequence_no=?",
                (stream_id, expected - 1)).fetchone()
            if previous is None or previous["event_id"] != event["previous_event_id"]:
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE stream_watermarks SET state='BLOCKED', updated_at=? "
                        "WHERE consumer_id=? AND stream_id=?", (now, consumer_id, stream_id))
                return DispatchResult("BLOCKED", reason="PREVIOUS_EVENT_MISMATCH")

        already = self.store.connection.execute(
            "SELECT result_sha256 FROM event_consumptions "
            "WHERE consumer_id=? AND event_id=?", (consumer_id, event["event_id"])
        ).fetchone()
        if already is not None:
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE stream_watermarks SET last_consumed_sequence=?, state='READY', "
                    "updated_at=? WHERE consumer_id=? AND stream_id=?",
                    (expected, now, consumer_id, stream_id))
            return DispatchResult("ALREADY_CONSUMED", event_id=event["event_id"])

        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT result_sha256 FROM event_consumptions "
                "WHERE consumer_id=? AND event_id=?",
                (consumer_id, event["event_id"])).fetchone()
            if current is None:
                event_value = dict(event)
                event_value["payload"] = json.loads(event_value.pop("payload_json"))
                result = handler(event_value, connection)
                result_hash = sha256_json(result if result is not None else {})
                connection.execute(
                    "INSERT INTO event_consumptions "
                    "(consumer_id, event_id, consumed_at, result_sha256) "
                    "VALUES (?, ?, ?, ?)",
                    (consumer_id, event["event_id"], now, result_hash))
                status = "CONSUMED"
            else:
                status = "ALREADY_CONSUMED"
            connection.execute(
                "UPDATE stream_watermarks SET last_consumed_sequence=?, state='READY', "
                "updated_at=? WHERE consumer_id=? AND stream_id=?",
                (expected, now, consumer_id, stream_id))
        return DispatchResult(status,
                              event_id=event["event_id"], sequence_no=expected)

    def recover(self, handlers, now, limit_per_consumer=100):
        """Drain persisted events after restart; handlers maps consumer_id to callable."""
        totals = {}
        for consumer_id, handler in sorted(handlers.items()):
            consumed = 0
            terminal = "IDLE"
            for unused in range(int(limit_per_consumer)):
                result = self.dispatch_next(consumer_id, handler, now)
                terminal = result.status
                if result.status not in {"CONSUMED", "ALREADY_CONSUMED"}:
                    break
                consumed += 1
            totals[consumer_id] = {"processed": consumed, "terminal_status": terminal}
        return totals


def classify_late_event(event_type, *, business_time, watermark_time,
                        committed_business_fact):
    if business_time >= watermark_time:
        return {"classification": "ON_TIME", "action": "PROCESS"}
    if committed_business_fact:
        return {
            "classification": "LATE_AFTER_COMMITTED_FACT",
            "action": "APPEND_AUDIT_CORRECTION_ONLY",
            "event_type": event_type,
        }
    return {
        "classification": "LATE_BEFORE_COMMITTED_FACT",
        "action": "APPEND_EXPLICIT_CORRECTION",
        "event_type": event_type,
    }
