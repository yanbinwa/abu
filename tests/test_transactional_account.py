import tempfile
import threading
import unittest
from pathlib import Path

from abupy.ServiceBu import (
    AccountCommandRejected, AccountEventEffects, AccountVersionConflict,
    DomainEventStore,
    OperationalStore, StrategyAccountStore, TransactionalAccountRepository,
    build_domain_event,
)


NOW = "2026-10-09T09:20:00+08:00"


class TransactionalAccountRepositoryTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = OperationalStore(
            Path(self.directory.name) / "operational.sqlite3")
        registry = StrategyAccountStore(self.store)
        registry.register_strategy_instance(
            "strategy-instance-a", "fixture", "v1", NOW)
        registry.register_account(
            "account-a", "Account A", "strategy-instance-a", NOW,
            initial_cash_micros=1_000_000)
        registry.register_account(
            "account-b", "Account B", "strategy-instance-a", NOW,
            initial_cash_micros=2_000_000)
        self.accounts = TransactionalAccountRepository(self.store)
        self.events = DomainEventStore(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    @staticmethod
    def add_cash(amount):
        def command(connection, context):
            connection.execute(
                "UPDATE account_balances SET cash_micros=cash_micros+?, "
                "updated_at=? WHERE account_id=?",
                (amount, NOW, context.account_id))
            return {"amount": amount, "next_version": context.next_version}
        return command

    def balance(self, account_id):
        return self.store.connection.execute(
            "SELECT cash_micros FROM account_balances WHERE account_id=?",
            (account_id,)).fetchone()[0]

    def append_event(self, suffix="a", account_id=None):
        event = build_domain_event(
            "FixtureEvent", "fixture-stream-{}".format(suffix), 1,
            {"suffix": suffix}, occurred_at=NOW, available_at=NOW,
            source_service="test", account_id=account_id)
        self.events.append(event)
        return event

    def test_command_and_version_increment_commit_together(self):
        result = self.accounts.execute(
            "account-a", 0, NOW, self.add_cash(500))
        self.assertEqual(0, result.previous_version)
        self.assertEqual(1, result.account_version)
        self.assertEqual(1_000_500, self.balance("account-a"))
        self.assertEqual(1, self.accounts.account("account-a")["account_version"])

    def test_stale_version_never_calls_handler(self):
        called = []
        with self.assertRaisesRegex(AccountVersionConflict, "expected version"):
            self.accounts.execute(
                "account-a", 1, NOW,
                lambda unused_connection, unused_context: called.append(True))
        self.assertEqual([], called)
        self.assertEqual(1_000_000, self.balance("account-a"))

    def test_handler_failure_rolls_back_domain_change_and_version(self):
        def broken(connection, context):
            self.add_cash(500)(connection, context)
            raise RuntimeError("fault after domain mutation")

        with self.assertRaisesRegex(RuntimeError, "fault after"):
            self.accounts.execute("account-a", 0, NOW, broken)
        self.assertEqual(1_000_000, self.balance("account-a"))
        self.assertEqual(0, self.accounts.account("account-a")["account_version"])

    def test_same_account_workers_are_serial_and_stale_work_loses(self):
        barrier = threading.Barrier(4)
        results = []
        failures = []

        def worker():
            barrier.wait()
            try:
                results.append(self.accounts.execute(
                    "account-a", 0, NOW, self.add_cash(100)))
            except AccountVersionConflict as error:
                failures.append(error)

        workers = [threading.Thread(target=worker) for unused in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(1, len(results))
        self.assertEqual(3, len(failures))
        self.assertEqual(1_000_100, self.balance("account-a"))
        self.assertEqual(1, self.accounts.account("account-a")["account_version"])

    def test_one_account_failure_does_not_change_another(self):
        def fail(unused_connection, unused_context):
            raise RuntimeError("account-a failed")

        with self.assertRaises(RuntimeError):
            self.accounts.execute("account-a", 0, NOW, fail)
        result = self.accounts.execute(
            "account-b", 0, NOW, self.add_cash(700))
        self.assertEqual(1, result.account_version)
        self.assertEqual(1_000_000, self.balance("account-a"))
        self.assertEqual(2_000_700, self.balance("account-b"))

    def test_blocked_account_rejects_commands(self):
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE accounts SET status='BLOCKED' WHERE account_id='account-a'")
        with self.assertRaisesRegex(AccountCommandRejected, "BLOCKED"):
            self.accounts.execute("account-a", 0, NOW, self.add_cash(1))
        self.assertEqual(0, self.accounts.account("account-a")["account_version"])

    def test_event_replay_returns_original_result_without_calling_handler(self):
        event = self.append_event()
        called = []

        def apply(connection, context, input_event):
            called.append(input_event["event_id"])
            self.add_cash(250)(connection, context)
            return AccountEventEffects(
                account_event_type="FixtureApplied",
                account_event_payload={"credited_micros": 250},
                value={"credited_micros": 250},
                notification_parts=("TEXT", "CHART_IMAGE"),
                trading_session=20261009)

        first = self.accounts.execute_event(
            "account-a", event["event_id"], 0, NOW, apply)
        replay = self.accounts.execute_event(
            "account-a", event["event_id"], 0, NOW, apply)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.value, replay.value)
        self.assertEqual([event["event_id"]], called)
        self.assertEqual(1_000_250, self.balance("account-a"))
        self.assertEqual(1, self.accounts.account("account-a")["account_version"])
        self.assertEqual(1, self.store.connection.execute(
            "SELECT count(*) FROM processed_events").fetchone()[0])
        self.assertEqual(1, self.store.connection.execute(
            "SELECT count(*) FROM account_events").fetchone()[0])
        self.assertEqual(1, self.store.connection.execute(
            "SELECT count(*) FROM domain_events WHERE stream_id='account:account-a'"
        ).fetchone()[0])
        watermark = self.store.connection.execute(
            "SELECT * FROM stream_watermarks WHERE stream_id=?",
            (event["stream_id"],)).fetchone()
        self.assertEqual(1, watermark["last_consumed_sequence"])
        self.assertEqual(1, self.store.connection.execute(
            "SELECT count(*) FROM notification_outbox").fetchone()[0])
        self.assertEqual(2, self.store.connection.execute(
            "SELECT count(*) FROM notification_parts").fetchone()[0])

    def test_event_handler_or_result_serialization_failure_rolls_back(self):
        event = self.append_event("broken")

        def broken(connection, context, unused_event):
            self.add_cash(250)(connection, context)
            return AccountEventEffects(
                account_event_type="BrokenFixture",
                account_event_payload={"mutation": "credit"},
                value={"not_json": {1, 2}})

        with self.assertRaises(TypeError):
            self.accounts.execute_event(
                "account-a", event["event_id"], 0, NOW, broken)
        self.assertEqual(1_000_000, self.balance("account-a"))
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM processed_events").fetchone()[0])
        self.assertEqual(0, self.accounts.account("account-a")["account_version"])

    def test_account_scoped_event_cannot_cross_account(self):
        event = self.append_event("private", account_id="account-b")
        with self.assertRaisesRegex(ValueError, "another account"):
            self.accounts.execute_event(
                "account-a", event["event_id"], 0, NOW,
                lambda unused_connection, unused_context, unused_event:
                AccountEventEffects("ShouldNotRun", {}, {}))

    def test_concurrent_same_event_is_applied_once(self):
        event = self.append_event("concurrent")
        barrier = threading.Barrier(4)
        called = []
        results = []

        def handler(connection, context, unused_event):
            called.append(True)
            self.add_cash(10)(connection, context)
            return AccountEventEffects(
                account_event_type="ConcurrentFixtureApplied",
                account_event_payload={"credited_micros": 10},
                value={"credited_micros": 10})

        def worker():
            barrier.wait()
            results.append(self.accounts.execute_event(
                "account-a", event["event_id"], 0, NOW, handler))

        workers = [threading.Thread(target=worker) for unused in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(1, len(called))
        self.assertEqual(1, sum(not item.replayed for item in results))
        self.assertEqual(3, sum(item.replayed for item in results))
        self.assertEqual(1_000_010, self.balance("account-a"))

    def test_invalid_notification_rolls_back_every_commit_component(self):
        event = self.append_event("invalid-notification")

        def handler(connection, context, unused_event):
            self.add_cash(10)(connection, context)
            return AccountEventEffects(
                account_event_type="InvalidNotificationFixture",
                account_event_payload={"credited_micros": 10},
                value={"credited_micros": 10},
                notification_parts=("VIDEO",))

        with self.assertRaisesRegex(ValueError, "notification part"):
            self.accounts.execute_event(
                "account-a", event["event_id"], 0, NOW, handler)
        self.assertEqual(1_000_000, self.balance("account-a"))
        for table in (
                "processed_events", "account_events", "event_consumptions",
                "notification_outbox", "notification_parts"):
            self.assertEqual(0, self.store.connection.execute(
                "SELECT count(*) FROM {}".format(table)).fetchone()[0])
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM domain_events WHERE stream_id='account:account-a'"
        ).fetchone()[0])
        self.assertEqual(0, self.accounts.account("account-a")["account_version"])

    def test_stream_gap_rolls_back_consumer_registration(self):
        first = self.append_event("gap")
        second = build_domain_event(
            "FixtureEvent", first["stream_id"], 2, {"suffix": "gap-2"},
            previous_event_id=first["event_id"], occurred_at=NOW,
            available_at=NOW, source_service="test")
        self.events.append(second)
        with self.assertRaisesRegex(ValueError, "expected sequence 1"):
            self.accounts.execute_event(
                "account-a", second["event_id"], 0, NOW,
                lambda unused_connection, unused_context, unused_event:
                AccountEventEffects("GapFixture", {}, {}))
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM event_consumers").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
