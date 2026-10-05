from __future__ import absolute_import

import json
import uuid

from .ABuContentStore import sha256_json


class EventCollisionError(ValueError):
    pass


class StreamSequenceError(ValueError):
    pass


def build_domain_event(event_type, stream_id, sequence_no, payload, *,
                       occurred_at, available_at, source_service,
                       previous_event_id=None, trading_session=None,
                       snapshot_id=None, account_id=None,
                       actor_strategy_instance_id=None,
                       actor_activation_id=None, affected_trade_ids=(),
                       affected_management_activation_ids=(), causation_id=None,
                       correlation_id=None, source_transaction_id=None):
    payload_sha256 = sha256_json(payload)
    identity = {
        "event_type": event_type,
        "stream_id": stream_id,
        "sequence_no": int(sequence_no),
        "payload_sha256": payload_sha256,
    }
    event_id = "evt-{}".format(sha256_json(identity)[:32])
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": "domain_event_v1",
        "stream_id": stream_id,
        "sequence_no": int(sequence_no),
        "previous_event_id": previous_event_id,
        "occurred_at": occurred_at,
        "available_at": available_at,
        "trading_session": trading_session,
        "source_service": source_service,
        "snapshot_id": snapshot_id,
        "account_id": account_id,
        "actor_strategy_instance_id": actor_strategy_instance_id,
        "actor_activation_id": actor_activation_id,
        "affected_trade_ids": sorted(set(affected_trade_ids)),
        "affected_management_activation_ids": sorted(
            set(affected_management_activation_ids)),
        "causation_id": causation_id,
        "correlation_id": correlation_id,
        "payload_sha256": payload_sha256,
        "payload": payload,
        "source_transaction_id": source_transaction_id or "tx-{}".format(uuid.uuid4().hex),
        "created_at": available_at,
    }


class DomainEventStore(object):

    def __init__(self, operational_store):
        self.store = operational_store

    @staticmethod
    def _event_tuple(event):
        return (
            event["event_id"], event["stream_id"], int(event["sequence_no"]),
            event.get("previous_event_id"), event["event_type"],
            event.get("schema_version", "domain_event_v1"), event["occurred_at"],
            event["available_at"], event.get("trading_session"),
            event["source_service"], event.get("snapshot_id"),
            event.get("account_id"), event.get("actor_strategy_instance_id"),
            event.get("actor_activation_id"),
            json.dumps(event.get("affected_trade_ids", []), sort_keys=True,
                       separators=(",", ":")),
            json.dumps(event.get("affected_management_activation_ids", []),
                       sort_keys=True, separators=(",", ":")),
            event.get("causation_id"), event.get("correlation_id"),
            event["payload_sha256"],
            json.dumps(event["payload"], ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")),
            event["source_transaction_id"], event["created_at"],
        )

    def append(self, event, connection=None):
        if sha256_json(event["payload"]) != event["payload_sha256"]:
            error = EventCollisionError("event payload hash mismatch")
            if connection is None:
                self._block_colliding_stream(event, str(error))
            raise error
        if connection is None:
            try:
                with self.store.transaction() as transaction:
                    return self.append(event, connection=transaction)
            except EventCollisionError as error:
                self._block_colliding_stream(event, str(error))
                raise

        existing = connection.execute(
            "SELECT * FROM domain_events WHERE event_id=?", (event["event_id"],)
        ).fetchone()
        if existing is not None:
            if (existing["stream_id"] == event["stream_id"] and
                    existing["sequence_no"] == int(event["sequence_no"]) and
                    existing["payload_sha256"] == event["payload_sha256"] and
                    existing["event_type"] == event["event_type"]):
                return dict(existing), False
            raise EventCollisionError("same event_id has different immutable content")

        last = connection.execute(
            "SELECT event_id, sequence_no FROM domain_events "
            "WHERE stream_id=? ORDER BY sequence_no DESC LIMIT 1",
            (event["stream_id"],)).fetchone()
        expected_sequence = 1 if last is None else last["sequence_no"] + 1
        expected_previous = None if last is None else last["event_id"]
        if int(event["sequence_no"]) != expected_sequence:
            raise StreamSequenceError(
                "stream {} expected sequence {}, got {}".format(
                    event["stream_id"], expected_sequence, event["sequence_no"]))
        if event.get("previous_event_id") != expected_previous:
            raise StreamSequenceError("previous_event_id does not match stream head")

        connection.execute(
            "INSERT INTO domain_events "
            "(event_id, stream_id, sequence_no, previous_event_id, event_type, "
            "schema_version, occurred_at, available_at, trading_session, source_service, "
            "snapshot_id, account_id, actor_strategy_instance_id, actor_activation_id, "
            "affected_trade_ids_json, affected_management_activation_ids_json, "
            "causation_id, correlation_id, payload_sha256, payload_json, "
            "source_transaction_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._event_tuple(event))
        return dict(connection.execute(
            "SELECT * FROM domain_events WHERE event_id=?", (event["event_id"],)
        ).fetchone()), True

    def _block_colliding_stream(self, event, detail):
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE stream_watermarks SET state='BLOCKED', updated_at=? "
                "WHERE stream_id=? AND state!='RETIRED'",
                (event.get("available_at") or event.get("created_at"), event["stream_id"]))
            existing = connection.execute(
                "SELECT event_id FROM domain_events WHERE event_id=?",
                (event.get("event_id"),)).fetchone()
            connection.execute(
                "INSERT OR IGNORE INTO audit_findings "
                "(finding_id, severity, category, event_id, detail_json, created_at) "
                "VALUES (?, 'CRITICAL', 'DOMAIN_EVENT_COLLISION', ?, ?, ?)",
                ("collision-{}".format(event.get("event_id", "unknown")),
                 None if existing is None else existing["event_id"],
                 json.dumps({
                     "stream_id": event["stream_id"],
                     "event_id": event.get("event_id"),
                     "detail": detail,
                 }, sort_keys=True),
                 event.get("available_at") or event.get("created_at")))

    def register_consumer(self, consumer_id, stream_id, effective_from_sequence,
                          *, required, retention_class, created_at):
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM event_consumers WHERE consumer_id=?", (consumer_id,)
            ).fetchone()
            expected = (stream_id, int(effective_from_sequence), int(bool(required)),
                        retention_class)
            if existing is not None:
                actual = (existing["effective_from_stream"],
                          existing["effective_from_sequence"], existing["required"],
                          existing["retention_class"])
                if actual != expected:
                    raise ValueError("consumer registration is immutable")
                return dict(existing), False
            connection.execute(
                "INSERT INTO event_consumers "
                "(consumer_id, required, effective_from_stream, effective_from_sequence, "
                "retention_class, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (consumer_id, int(bool(required)), stream_id,
                 int(effective_from_sequence), retention_class, created_at))
            connection.execute(
                "INSERT INTO stream_watermarks "
                "(consumer_id, stream_id, last_consumed_sequence, state, updated_at) "
                "VALUES (?, ?, ?, 'READY', ?)",
                (consumer_id, stream_id, int(effective_from_sequence) - 1, created_at))
            return dict(connection.execute(
                "SELECT * FROM event_consumers WHERE consumer_id=?", (consumer_id,)
            ).fetchone()), True

    def retire_consumer(self, consumer_id, retired_at, retired_after_sequence=None,
                        *, approved_gap=False):
        with self.store.transaction() as connection:
            consumer = connection.execute(
                "SELECT * FROM event_consumers WHERE consumer_id=?", (consumer_id,)
            ).fetchone()
            if consumer is None:
                raise KeyError("unknown event consumer: {}".format(consumer_id))
            watermark = connection.execute(
                "SELECT * FROM stream_watermarks WHERE consumer_id=? AND stream_id=?",
                (consumer_id, consumer["effective_from_stream"])).fetchone()
            boundary = (watermark["last_consumed_sequence"] if retired_after_sequence is None
                        else int(retired_after_sequence))
            if watermark["last_consumed_sequence"] < boundary and not approved_gap:
                raise ValueError("consumer has not reached retirement boundary")
            connection.execute(
                "UPDATE event_consumers SET retired_at=?, retired_after_sequence=? "
                "WHERE consumer_id=?", (retired_at, boundary, consumer_id))
            connection.execute(
                "UPDATE stream_watermarks SET state='RETIRED', updated_at=? "
                "WHERE consumer_id=?", (retired_at, consumer_id))
            if approved_gap:
                connection.execute(
                    "INSERT INTO audit_findings "
                    "(finding_id, severity, category, detail_json, created_at) "
                    "VALUES (?, 'WARNING', 'CONSUMER_RETIREMENT_GAP', ?, ?)",
                    ("finding-{}".format(uuid.uuid4().hex), json.dumps({
                        "consumer_id": consumer_id,
                        "retired_after_sequence": boundary,
                        "last_consumed_sequence": watermark["last_consumed_sequence"],
                    }, sort_keys=True), retired_at))
            return boundary

    def unconsumed(self, consumer_id, limit=100):
        consumer = self.store.connection.execute(
            "SELECT * FROM event_consumers WHERE consumer_id=?", (consumer_id,)
        ).fetchone()
        if consumer is None:
            raise KeyError("unknown event consumer: {}".format(consumer_id))
        watermark = self.store.connection.execute(
            "SELECT last_consumed_sequence FROM stream_watermarks "
            "WHERE consumer_id=? AND stream_id=?",
            (consumer_id, consumer["effective_from_stream"])).fetchone()
        start = max(consumer["effective_from_sequence"],
                    watermark["last_consumed_sequence"] + 1)
        rows = self.store.connection.execute(
            "SELECT * FROM domain_events WHERE stream_id=? AND sequence_no>=? "
            "ORDER BY sequence_no LIMIT ?",
            (consumer["effective_from_stream"], start, int(limit))).fetchall()
        return [dict(row) for row in rows]

    def archive_eligibility(self, stream_id, through_sequence):
        blockers = []
        consumers = self.store.connection.execute(
            "SELECT * FROM event_consumers WHERE required=1 "
            "AND effective_from_stream=?", (stream_id,)).fetchall()
        for consumer in consumers:
            relevant_end = int(through_sequence)
            if consumer["retired_at"] is not None:
                relevant_end = min(relevant_end, consumer["retired_after_sequence"])
            if relevant_end < consumer["effective_from_sequence"]:
                continue
            watermark = self.store.connection.execute(
                "SELECT last_consumed_sequence FROM stream_watermarks "
                "WHERE consumer_id=? AND stream_id=?",
                (consumer["consumer_id"], stream_id)).fetchone()
            consumed = -1 if watermark is None else watermark["last_consumed_sequence"]
            if consumed < relevant_end:
                blockers.append({
                    "consumer_id": consumer["consumer_id"],
                    "required_sequence": relevant_end,
                    "last_consumed_sequence": consumed,
                })
        return {"eligible": not blockers, "blockers": blockers,
                "automatic_delete": False}

    def record_archive_review(self, stream_id, through_sequence, *, reviewed_by,
                              reviewed_at, approved):
        eligibility = self.archive_eligibility(stream_id, through_sequence)
        if approved and not eligibility["eligible"]:
            raise ValueError("required consumers have not reached archive boundary")
        detail = {
            "stream_id": stream_id,
            "through_sequence": int(through_sequence),
            "reviewed_by": reviewed_by,
            "approved": bool(approved),
            "eligibility": eligibility,
            "automatic_delete": False,
        }
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_findings "
                "(finding_id, severity, category, detail_json, created_at) "
                "VALUES (?, 'INFO', 'DOMAIN_EVENT_ARCHIVE_REVIEW', ?, ?)",
                ("archive-review-{}".format(uuid.uuid4().hex),
                 json.dumps(detail, sort_keys=True), reviewed_at))
        return detail
