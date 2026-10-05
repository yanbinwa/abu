# Paper service v1 contract rules

This directory contains M0 contracts only. It does not enable the service, account writes,
minute execution, or broker connectivity.

## Canonical encoding and identifiers

All hashes use UTF-8 JSON with sorted keys and separators `(',', ':')`. Timestamps must include
an offset and operational timestamps use `Asia/Shanghai`.

- `payload_sha256 = sha256(canonical_json(payload))`.
- `event_id = <event_type_slug>-<first 24 hex chars of sha256(schema_version + '|' +
  stream_id + '|' + sequence_no + '|' + payload_sha256)>`.
- A retry reuses the same event ID, stream sequence and payload hash.
- A correction is a new event with a new sequence and a causation link; it never changes an old event.
- `snapshot_id = <snapshot_type_slug>-<first 24 hex chars of manifest_sha256>`.
- `order_id`, `fill_id` and `trade_id` are only unique inside `account_id`; persistent keys always
  include the account namespace.
- Job idempotency keys use `<job_id>:<job_version>:<scheduled_business_key>`.
- Account phase idempotency keys use
  `account-phase:<account_id>:<trading_session>:<target_phase>:<input_snapshot_id>`.

## Database migrations

1. Migrations are monotonic and run inside an exclusive transaction before the scheduler starts.
2. The service refuses a database newer than the code-supported schema.
3. A failed migration rolls back completely and prevents service startup.
4. Downgrades and destructive migrations are not supported in v1.
5. Before a migration, create and verify a SQLite Online Backup plus backup manifest.
6. `operational_v1.sql` is idempotent for an empty or already-created v1 database; it is not a
   substitute for later version-to-version migration scripts.
7. The active data-only runtime continues to request schema v1. Account shadow work must explicitly
   request schema v2 after a verified backup; v2 adds durable cash/share receivables and sell-share
   reservations without changing v1 tables in place.
8. Schema v3 adds the immutable account daily-close projection. It is a separate migration because
   published migration files are never edited after release.
9. Schema v4 adds per-attempt notification delivery history and audited operator actions; v3 remains
   immutable.

## Transaction boundaries

Publishing a market snapshot commits the `COMMITTED` catalog row and its `domain_event` in one
SQLite transaction. Applying an account input commits account state, processed-event identity,
stream watermark, downstream domain events and notification outbox rows in one transaction.
Network I/O and artifact rendering never run inside account transactions.

## Stable streams

- Minute stream: `market-minute:<YYYYMMDD>:<interval_minutes>`.
- A watchlist change increments `watchlist_version` but never changes the current session stream.
- Daily stream: `market-daily:<YYYYMMDD>`.
- Account stream: `account:<account_id>`.

Consumers only advance consecutive sequence numbers. A gap blocks trade-critical streams.
Late market revisions are audit-only after an order transition has committed.

## Account session phases

The only valid forward path is:

```text
CREATED
-> PREOPEN_INPUTS_READY
-> RECEIVABLES_APPLIED
-> OPEN_SELLS_PROCESSED
-> INTRADAY_BUYS_ENABLED
-> DAILY_CLOSE_COMPLETED
```

No transition may skip a phase. `PREOPEN_INPUTS_READY` requires one committed preopen snapshot
with no missing required input. Restart repeats the same phase idempotency key and cannot apply
receivables, corporate actions or opening sells twice.

## Activation ownership

The account active activation governs new decisions. Each logical trade separately freezes its
opening activation, management activation, entry policy, exit policy and risk policy. A takeover
requires a `PositionManagementTakenOver` event and a new management assignment row.

## Consumer lifecycle and retention

Consumers start confirmation at their registered effective sequence. Required consumers block
archive until confirmation or an audited retirement boundary. v1 never automatically deletes
domain events. Only rebuildable artifacts may be automatically cleaned.

## Cutover boundary

Before the first new-system account event, the new writer may be stopped and the unchanged legacy
writer restored. After the first new-system account event, restoring the frozen legacy snapshot is
forbidden. Recovery must move forward using the latest authoritative database or an independently
reviewed reverse migration of that latest state.
