from __future__ import absolute_import

import json
from pathlib import Path

from ..MarketBu.ABuMinuteBarStore import MinuteBarStore
from .ABuMarketSnapshotCatalog import SnapshotCatalog
from .ABuMinuteMarketHub import MinuteSnapshotBatch
from .ABuTransactionalAccount import TransactionalAccountRepository
from .ABuTransactionalIntradayBroker import TransactionalIntradayBroker


class TransactionalMinuteExecutionJob(object):
    """Consume the next committed real minute snapshot into one shadow account."""

    def __init__(self, operational_store, snapshot_root, minute_store_root,
                 trading_calendar_path, account_id, execution_policy_id,
                 *, catalog=None, minute_store=None, broker=None):
        self.store = operational_store
        self.catalog = catalog or SnapshotCatalog(operational_store, snapshot_root)
        self.minute_store = minute_store or MinuteBarStore(minute_store_root)
        self.calendar = tuple(sorted(int(item) for item in json.loads(
            Path(trading_calendar_path).read_text(encoding="utf-8"))["dates"]))
        self.account_id = account_id
        self.accounts = TransactionalAccountRepository(operational_store)
        self.broker = broker or TransactionalIntradayBroker(
            self.accounts, execution_policy_id=execution_policy_id)

    def _next_session(self, session):
        later = [item for item in self.calendar if item > int(session)]
        if not later:
            raise ValueError("trading calendar has no next session")
        return later[0]

    def _batch(self, event):
        manifest = self.catalog.manifest(event["snapshot_id"])
        events_by_symbol = {}
        for symbol, record in manifest["symbols"].items():
            if record["terminal_status"] in ("AVAILABLE", "STALE"):
                events = tuple(self.minute_store.read_selected(record))
                if events:
                    events_by_symbol[symbol] = events
        return MinuteSnapshotBatch(
            snapshot_id=manifest["snapshot_id"],
            stream_id=manifest["stream_id"],
            sequence_no=int(manifest["sequence_no"]),
            previous_snapshot_id=manifest["previous_snapshot_id"],
            decision_cutoff=manifest["decision_cutoff"],
            watchlist_id=manifest["watchlist_id"],
            watchlist_version=int(manifest["watchlist_version"]),
            manifest=manifest, events_by_symbol=events_by_symbol)

    def __call__(self):
        session_row = self.store.connection.execute(
            "SELECT * FROM account_sessions WHERE account_id=? "
            "AND phase='INTRADAY_BUYS_ENABLED' AND blocked_reason IS NULL "
            "ORDER BY trading_session DESC LIMIT 1", (self.account_id,)).fetchone()
        if session_row is None:
            return {"status": "NO_INTRADAY_ACCOUNT_SESSION",
                    "output_snapshot_ids": []}
        session = int(session_row["trading_session"])
        event = self.store.connection.execute(
            "SELECT e.* FROM domain_events e WHERE e.event_type='MinuteSnapshotCommitted' "
            "AND e.trading_session=? AND NOT EXISTS (SELECT 1 FROM processed_events p "
            "WHERE p.account_id=? AND p.event_id=e.event_id) "
            "ORDER BY e.sequence_no LIMIT 1", (session, self.account_id)).fetchone()
        if event is None:
            return {"status": "NO_PENDING_MINUTE_SNAPSHOT",
                    "output_snapshot_ids": []}
        batch = self._batch(event)
        result = self.broker.process_snapshot_batch(
            self.account_id, event["event_id"],
            self.accounts.account(self.account_id)["account_version"],
            session, batch, batch.decision_cutoff,
            next_trading_session=self._next_session(session))
        return {"status": "ACCOUNT_SNAPSHOT_CONSUMED",
                "account_id": self.account_id,
                "account_version": result.account_version,
                "snapshot_id": batch.snapshot_id,
                "value": result.value,
                "output_snapshot_ids": [batch.snapshot_id]}
