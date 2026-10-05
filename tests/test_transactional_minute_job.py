import json
import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu import (
    DomainEventStore, OperationalStore, StrategyAccountStore,
    build_domain_event,
)
from abupy.ServiceBu.ABuTransactionalMinuteJob import TransactionalMinuteExecutionJob


NOW = "2026-10-09T09:35:00+08:00"


class FakeCatalog(object):
    def manifest(self, unused_snapshot_id):
        return {"snapshot_id": "minute-a", "stream_id": "market-minute:20261009:1",
                "sequence_no": 1, "previous_snapshot_id": None,
                "decision_cutoff": NOW, "watchlist_id": "w",
                "watchlist_version": 1, "symbols": {}}


class FakeBroker(object):
    def __init__(self):
        self.calls = []

    def process_snapshot_batch(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return type("Result", (), {"account_version": 7,
                                    "value": {"fills": []}})()


class TransactionalMinuteExecutionJobTest(unittest.TestCase):

    def test_next_real_snapshot_is_forwarded_with_next_trading_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calendar = root / "calendar.json"
            calendar.write_text(json.dumps({"dates": [20261009, 20261012]}))
            store = OperationalStore(root / "db.sqlite3", target_schema_version=4)
            registry = StrategyAccountStore(store)
            registry.register_strategy_instance("i", "s", "1", NOW)
            registry.register_account("a", "A", "i", NOW)
            registry.activate_strategy("x", "a", {}, 20261009,
                                       change_reason="test", approved_at=NOW)
            with store.transaction() as connection:
                connection.execute(
                    "INSERT INTO account_sessions "
                    "(account_id,trading_session,phase,phase_version,updated_at) "
                    "VALUES ('a',20261009,'INTRADAY_BUYS_ENABLED',4,?)", (NOW,))
                connection.execute(
                    "INSERT INTO market_snapshots "
                    "(snapshot_id,snapshot_type,stream_id,sequence_no,trading_session,"
                    "decision_cutoff,status,manifest_path,manifest_sha256,created_at,"
                    "committed_at,quality_codes_json) VALUES "
                    "('minute-a','MINUTE','market-minute:20261009:1',1,20261009,?,"
                    "'COMMITTED','/mock',?, ?,?,'[]')", (NOW, "a" * 64, NOW, NOW))
            event = build_domain_event(
                "MinuteSnapshotCommitted", "market-minute:20261009:1", 1,
                {"snapshot_id": "minute-a"}, occurred_at=NOW, available_at=NOW,
                source_service="test", trading_session=20261009,
                snapshot_id="minute-a")
            DomainEventStore(store).append(event)
            broker = FakeBroker()
            job = TransactionalMinuteExecutionJob(
                store, root, root, calendar, "a", "M1",
                catalog=FakeCatalog(), minute_store=object(), broker=broker)
            result = job()
            self.assertEqual("ACCOUNT_SNAPSHOT_CONSUMED", result["status"])
            self.assertEqual(20261012, broker.calls[0][1]["next_trading_session"])
            store.close()

    def test_without_enabled_session_does_not_touch_account(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calendar = root / "calendar.json"
            calendar.write_text(json.dumps({"dates": [20261009, 20261012]}))
            store = OperationalStore(root / "db.sqlite3", target_schema_version=4)
            broker = FakeBroker()
            result = TransactionalMinuteExecutionJob(
                store, root, root, calendar, "a", "M1",
                catalog=FakeCatalog(), minute_store=object(), broker=broker)()
            self.assertEqual("NO_INTRADAY_ACCOUNT_SESSION", result["status"])
            self.assertEqual([], broker.calls)
            store.close()


if __name__ == "__main__":
    unittest.main()
