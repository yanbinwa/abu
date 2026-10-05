from __future__ import absolute_import

import threading
import json
from contextlib import contextmanager
from dataclasses import dataclass

from .ABuContentStore import sha256_json
from .ABuDomainEventStore import DomainEventStore, build_domain_event


class AccountVersionConflict(RuntimeError):
    """The caller based its command on a stale account projection."""


class AccountCommandRejected(RuntimeError):
    """The account cannot accept commands in its current lifecycle state."""


@dataclass(frozen=True)
class AccountTransactionContext:
    account_id: str
    strategy_instance_id: str
    active_activation_id: str | None
    previous_version: int
    next_version: int
    status: str


@dataclass(frozen=True)
class AccountCommandResult:
    account_id: str
    previous_version: int
    account_version: int
    value: object


@dataclass(frozen=True)
class AccountEventCommandResult:
    account_id: str
    event_id: str
    previous_version: int
    account_version: int
    value: object
    replayed: bool


@dataclass(frozen=True)
class AccountEventEffects:
    account_event_type: str
    account_event_payload: dict
    value: object
    notification_parts: tuple[str, ...] = ()
    affected_trade_ids: tuple[str, ...] = ()
    affected_management_activation_ids: tuple[str, ...] = ()
    transitioned_order_ids: tuple[str, ...] = ()
    trading_session: int | None = None


class AccountCommandQueue(object):
    """In-process per-account serialization for the single service writer."""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks = {}

    def _lock_for(self, account_id):
        with self._guard:
            return self._locks.setdefault(str(account_id), threading.Lock())

    @contextmanager
    def serial(self, account_id):
        lock = self._lock_for(account_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()


class TransactionalAccountRepository(object):
    """Optimistic-versioned account command and SQLite commit boundary."""

    WRITABLE_STATUSES = frozenset(("SHADOW", "PAPER", "PAUSED"))

    def __init__(self, operational_store, command_queue=None):
        self.store = operational_store
        self.command_queue = command_queue or AccountCommandQueue()
        self.events = DomainEventStore(operational_store)

    @staticmethod
    def _load_account(connection, account_id):
        row = connection.execute(
            "SELECT * FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown account_id: {}".format(account_id))
        return row

    def account(self, account_id):
        with self.store.transaction(immediate=False) as connection:
            return dict(self._load_account(connection, account_id))

    def execute(self, account_id, expected_version, updated_at, handler):
        if not callable(handler):
            raise TypeError("account command handler must be callable")
        expected_version = int(expected_version)
        if expected_version < 0:
            raise ValueError("expected account version cannot be negative")
        if not updated_at:
            raise ValueError("updated_at is required")

        with self.command_queue.serial(account_id):
            with self.store.transaction() as connection:
                account = self._load_account(connection, account_id)
                current = int(account["account_version"])
                if current != expected_version:
                    raise AccountVersionConflict(
                        "account {} expected version {} but is {}".format(
                            account_id, expected_version, current))
                if account["status"] not in self.WRITABLE_STATUSES:
                    raise AccountCommandRejected(
                        "account {} is {}".format(account_id, account["status"]))
                context = AccountTransactionContext(
                    account_id=account_id,
                    strategy_instance_id=account["strategy_instance_id"],
                    active_activation_id=account["active_activation_id"],
                    previous_version=current,
                    next_version=current + 1,
                    status=account["status"])
                value = handler(connection, context)
                updated = connection.execute(
                    "UPDATE accounts SET account_version=?, updated_at=? "
                    "WHERE account_id=? AND account_version=?",
                    (context.next_version, updated_at, account_id, current))
                if updated.rowcount != 1:
                    raise AccountVersionConflict(
                        "account version changed during command")
                return AccountCommandResult(
                    account_id=account_id, previous_version=current,
                    account_version=context.next_version, value=value)

    @staticmethod
    def _consumer_id(account_id, stream_id):
        digest = sha256_json({
            "account_id": account_id, "stream_id": stream_id})[:20]
        return "account-consumer-{}-{}".format(account_id, digest)

    @staticmethod
    def _prepare_watermark(connection, account_id, event, processed_at,
                           effective_from_sequence):
        consumer_id = TransactionalAccountRepository._consumer_id(
            account_id, event["stream_id"])
        consumer = connection.execute(
            "SELECT * FROM event_consumers WHERE consumer_id=?", (consumer_id,)
        ).fetchone()
        if consumer is None:
            connection.execute(
                "INSERT INTO event_consumers "
                "(consumer_id, required, effective_from_stream, "
                "effective_from_sequence, retention_class, created_at) "
                "VALUES (?, 1, ?, ?, 'PERMANENT_AUDIT', ?)",
                (consumer_id, event["stream_id"],
                 int(effective_from_sequence), processed_at))
            connection.execute(
                "INSERT INTO stream_watermarks "
                "(consumer_id, stream_id, last_consumed_sequence, state, updated_at) "
                "VALUES (?, ?, ?, 'READY', ?)",
                (consumer_id, event["stream_id"],
                 int(effective_from_sequence) - 1, processed_at))
        else:
            expected_identity = (
                event["stream_id"], int(effective_from_sequence))
            actual_identity = (
                consumer["effective_from_stream"],
                int(consumer["effective_from_sequence"]))
            if actual_identity != expected_identity:
                raise ValueError("account event consumer identity changed")
        watermark = connection.execute(
            "SELECT * FROM stream_watermarks WHERE consumer_id=? AND stream_id=?",
            (consumer_id, event["stream_id"])).fetchone()
        if watermark["state"] != "READY":
            raise ValueError("account input stream is not READY")
        expected = int(watermark["last_consumed_sequence"]) + 1
        if int(event["sequence_no"]) != expected:
            raise ValueError(
                "account input stream expected sequence {}, got {}".format(
                    expected, event["sequence_no"]))
        return consumer_id

    def execute_event(self, account_id, event_id, expected_version,
                      processed_at, handler, *, effective_from_sequence=1):
        """Apply one immutable input event exactly once for this account."""
        if not callable(handler):
            raise TypeError("account event handler must be callable")
        expected_version = int(expected_version)
        if expected_version < 0:
            raise ValueError("expected account version cannot be negative")
        if not event_id or not processed_at:
            raise ValueError("event_id and processed_at are required")

        with self.command_queue.serial(account_id):
            with self.store.transaction() as connection:
                event = connection.execute(
                    "SELECT * FROM domain_events WHERE event_id=?", (event_id,)
                ).fetchone()
                if event is None:
                    raise KeyError("unknown event_id: {}".format(event_id))
                if event["account_id"] not in (None, account_id):
                    raise ValueError("event is scoped to another account")
                processed = connection.execute(
                    "SELECT * FROM processed_events WHERE account_id=? AND event_id=?",
                    (account_id, event_id)).fetchone()
                if processed is not None:
                    if processed["payload_sha256"] != event["payload_sha256"]:
                        raise ValueError("processed event payload hash mismatch")
                    envelope = json.loads(processed["result_json"])
                    return AccountEventCommandResult(
                        account_id=account_id, event_id=event_id,
                        previous_version=int(envelope["account_version"]) - 1,
                        account_version=int(envelope["account_version"]),
                        value=envelope["value"], replayed=True)

                account = self._load_account(connection, account_id)
                current = int(account["account_version"])
                if current != expected_version:
                    raise AccountVersionConflict(
                        "account {} expected version {} but is {}".format(
                            account_id, expected_version, current))
                if account["status"] not in self.WRITABLE_STATUSES:
                    raise AccountCommandRejected(
                        "account {} is {}".format(account_id, account["status"]))
                context = AccountTransactionContext(
                    account_id=account_id,
                    strategy_instance_id=account["strategy_instance_id"],
                    active_activation_id=account["active_activation_id"],
                    previous_version=current, next_version=current + 1,
                    status=account["status"])
                consumer_id = self._prepare_watermark(
                    connection, account_id, event, processed_at,
                    effective_from_sequence)
                effects = handler(connection, context, dict(event))
                if not isinstance(effects, AccountEventEffects):
                    raise TypeError(
                        "account event handler must return AccountEventEffects")
                if not effects.account_event_type:
                    raise ValueError("account event type is required")
                envelope = {
                    "account_version": context.next_version,
                    "value": effects.value,
                }
                encoded_result = json.dumps(
                    envelope, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False)
                encoded_account_payload = json.dumps(
                    effects.account_event_payload, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"), allow_nan=False)

                account_stream = "account:{}".format(account_id)
                head = connection.execute(
                    "SELECT event_id, sequence_no FROM domain_events "
                    "WHERE stream_id=? ORDER BY sequence_no DESC LIMIT 1",
                    (account_stream,)).fetchone()
                sequence = 1 if head is None else int(head["sequence_no"]) + 1
                output_event = build_domain_event(
                    effects.account_event_type, account_stream, sequence,
                    effects.account_event_payload,
                    previous_event_id=None if head is None else head["event_id"],
                    occurred_at=processed_at, available_at=processed_at,
                    source_service="transactional-paper-broker",
                    trading_session=effects.trading_session,
                    account_id=account_id,
                    actor_strategy_instance_id=context.strategy_instance_id,
                    actor_activation_id=context.active_activation_id,
                    affected_trade_ids=effects.affected_trade_ids,
                    affected_management_activation_ids=(
                        effects.affected_management_activation_ids),
                    causation_id=event_id,
                    correlation_id=event["correlation_id"])
                output_row, unused_created = self.events.append(
                    output_event, connection=connection)
                execution_states = effects.account_event_payload.get(
                    "execution_states", {})
                for order_id in sorted(set(effects.transitioned_order_ids)):
                    if order_id not in execution_states:
                        raise ValueError(
                            "transitioned order has no persisted machine state")
                    updated_state = connection.execute(
                        "UPDATE order_execution_states "
                        "SET last_transition_event_id=?, updated_at=? "
                        "WHERE account_id=? AND order_id=?",
                        (output_row["event_id"], processed_at,
                         account_id, order_id))
                    if updated_state.rowcount != 1:
                        raise ValueError(
                            "transitioned order execution state is missing")
                account_event_id = "account-event-{}".format(sha256_json({
                    "account_id": account_id, "event_id": output_row["event_id"],
                    "account_version": context.next_version,
                })[:24])
                connection.execute(
                    "INSERT INTO account_events "
                    "(account_event_id, account_id, account_version, event_type, "
                    "event_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (account_event_id, account_id, context.next_version,
                     effects.account_event_type, output_row["event_id"],
                     encoded_account_payload, processed_at))
                connection.execute(
                    "INSERT INTO processed_events "
                    "(account_id, event_id, payload_sha256, processed_at, result_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (account_id, event_id, event["payload_sha256"],
                     processed_at, encoded_result))
                connection.execute(
                    "INSERT INTO event_consumptions "
                    "(consumer_id, event_id, consumed_at, result_sha256) "
                    "VALUES (?, ?, ?, ?)",
                    (consumer_id, event_id, processed_at,
                     sha256_json(envelope)))
                connection.execute(
                    "UPDATE stream_watermarks SET last_consumed_sequence=?, "
                    "state='READY', updated_at=? "
                    "WHERE consumer_id=? AND stream_id=?",
                    (int(event["sequence_no"]), processed_at, consumer_id,
                     event["stream_id"]))
                if effects.notification_parts:
                    allowed_parts = frozenset(("TEXT", "CHART_IMAGE"))
                    parts = tuple(sorted(set(effects.notification_parts)))
                    if any(part not in allowed_parts for part in parts):
                        raise ValueError("unknown notification part")
                    notification_id = "notification-{}".format(
                        output_row["event_id"])
                    connection.execute(
                        "INSERT INTO notification_outbox "
                        "(account_id, notification_event_id, source_event_id, "
                        "status, created_at, updated_at) "
                        "VALUES (?, ?, ?, 'PENDING', ?, ?)",
                        (account_id, notification_id, output_row["event_id"],
                         processed_at, processed_at))
                    for part in parts:
                        connection.execute(
                            "INSERT INTO notification_parts "
                            "(notification_event_id, account_id, part_kind, status, "
                            "attempt_count, updated_at) "
                            "VALUES (?, ?, ?, 'PENDING', 0, ?)",
                            (notification_id, account_id, part, processed_at))
                updated = connection.execute(
                    "UPDATE accounts SET account_version=?, updated_at=? "
                    "WHERE account_id=? AND account_version=?",
                    (context.next_version, processed_at, account_id, current))
                if updated.rowcount != 1:
                    raise AccountVersionConflict(
                        "account version changed during event command")
                return AccountEventCommandResult(
                    account_id=account_id, event_id=event_id,
                    previous_version=current,
                    account_version=context.next_version,
                    value=effects.value, replayed=False)
