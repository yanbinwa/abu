import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu import (
    AccountCommandRejected, AccountEventEffects, AccountSessionCoordinator,
    AccountSessionStore,
    DomainEventStore, InvalidAccountSessionTransition, OperationalStore,
    PreopenSnapshotBuilder, SnapshotCatalog, StrategyAccountStore,
    TransactionalAccountRepository, build_domain_event,
)


NOW = "2026-10-09T09:20:00+08:00"
SESSION = 20261009


class AccountSessionCoordinatorTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = OperationalStore(root / "operational.sqlite3")
        registry = StrategyAccountStore(self.store)
        registry.register_strategy_instance("instance-a", "fixture", "v1", NOW)
        registry.register_account(
            "account-a", "Account A", "instance-a", NOW,
            initial_cash_micros=1_000_000)
        registry.activate_strategy(
            "activation-a", "account-a", {"version": 1}, SESSION,
            change_reason="fixture", approved_at=NOW)
        self.accounts = TransactionalAccountRepository(self.store)
        self.sessions = AccountSessionStore()
        self.coordinator = AccountSessionCoordinator(
            self.accounts, self.sessions)
        self.events = DomainEventStore(self.store)
        self.preopen = PreopenSnapshotBuilder(
            SnapshotCatalog(self.store, root / "content"))
        self.accounts.execute(
            "account-a", 0, NOW,
            lambda connection, context: self.sessions.create(
                connection, context, SESSION, NOW))

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def session(self):
        return dict(self.store.connection.execute(
            "SELECT * FROM account_sessions "
            "WHERE account_id='account-a' AND trading_session=?", (SESSION,)
        ).fetchone())

    def balance(self):
        return self.store.connection.execute(
            "SELECT cash_micros FROM account_balances "
            "WHERE account_id='account-a'").fetchone()[0]

    def publish_preopen(self):
        return self.preopen.publish(
            trading_session=SESSION, decision_cutoff=NOW, created_at=NOW,
            security_master_version="security-v1",
            corporate_action_version="actions-v1",
            security_status_version="status-v1",
            limit_reference_version="limits-v1", receivables_cutoff=NOW,
            pending_order_snapshot_id="orders-v1")

    def append_phase_event(self, sequence, event_type, previous_event_id=None):
        event = build_domain_event(
            event_type,
            "account-phase-input:account-a:{}".format(SESSION), sequence,
            {"phase_command": event_type},
            previous_event_id=previous_event_id, occurred_at=NOW,
            available_at=NOW, source_service="test", trading_session=SESSION,
            account_id="account-a", correlation_id="session:{}".format(SESSION))
        self.events.append(event)
        return event

    def advance_to_preopen(self):
        snapshot, event, unused_created = self.publish_preopen()
        result = self.coordinator.accept_preopen_inputs(
            "account-a", event["event_id"], 1, SESSION, 0,
            snapshot["snapshot_id"], NOW)
        return snapshot, event, result

    def test_phase_actions_commit_once_and_replay_without_domain_mutation(self):
        unused_snapshot, unused_event, ready = self.advance_to_preopen()
        self.assertEqual("PREOPEN_INPUTS_READY", ready.value["phase"])
        receivables = self.append_phase_event(
            1, "ApplyReceivablesAndCorporateActions")
        calls = []

        def credit(connection, context, unused_input):
            calls.append(True)
            connection.execute(
                "UPDATE account_balances SET cash_micros=cash_micros+100, "
                "updated_at=? WHERE account_id=?", (NOW, context.account_id))
            return {"cash_credited_micros": 100, "corporate_actions": 0}

        first = self.coordinator.apply_receivables_and_corporate_actions(
            "account-a", receivables["event_id"], 2, SESSION, 1, NOW, credit)
        replay = self.coordinator.apply_receivables_and_corporate_actions(
            "account-a", receivables["event_id"], 2, SESSION, 1, NOW, credit)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual([True], calls)
        self.assertEqual(1_000_100, self.balance())
        self.assertEqual("RECEIVABLES_APPLIED", self.session()["phase"])
        self.assertEqual(3, self.accounts.account("account-a")["account_version"])

    def test_failure_rolls_back_domain_change_phase_event_and_watermark(self):
        self.advance_to_preopen()
        event = self.append_phase_event(
            1, "ApplyReceivablesAndCorporateActions")

        def broken(connection, context, unused_input):
            connection.execute(
                "UPDATE account_balances SET cash_micros=1 "
                "WHERE account_id=?", (context.account_id,))
            raise RuntimeError("fault during receivables")

        with self.assertRaisesRegex(RuntimeError, "fault during"):
            self.coordinator.apply_receivables_and_corporate_actions(
                "account-a", event["event_id"], 2, SESSION, 1, NOW, broken)
        self.assertEqual(1_000_000, self.balance())
        self.assertEqual("PREOPEN_INPUTS_READY", self.session()["phase"])
        self.assertEqual(2, self.accounts.account("account-a")["account_version"])
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM processed_events WHERE event_id=?",
            (event["event_id"],)).fetchone()[0])

    def test_different_event_cannot_declare_completed_phase_again(self):
        self.advance_to_preopen()
        first = self.append_phase_event(
            1, "ApplyReceivablesAndCorporateActions")
        self.coordinator.apply_receivables_and_corporate_actions(
            "account-a", first["event_id"], 2, SESSION, 1, NOW,
            lambda unused_connection, unused_context, unused_event: {})
        duplicate = self.append_phase_event(
            2, "ApplyReceivablesAgain", first["event_id"])
        with self.assertRaisesRegex(ValueError, "different input event"):
            self.coordinator.apply_receivables_and_corporate_actions(
                "account-a", duplicate["event_id"], 3, SESSION, 2, NOW,
                lambda unused_connection, unused_context, unused_event: {})
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM processed_events WHERE event_id=?",
            (duplicate["event_id"],)).fetchone()[0])

    def enable_buys(self):
        self.advance_to_preopen()
        receivables = self.append_phase_event(
            1, "ApplyReceivablesAndCorporateActions")
        self.coordinator.apply_receivables_and_corporate_actions(
            "account-a", receivables["event_id"], 2, SESSION, 1, NOW,
            lambda unused_connection, unused_context, unused_event: {})
        sells = self.append_phase_event(
            2, "ProcessOpenSells", receivables["event_id"])
        self.coordinator.process_open_sells(
            "account-a", sells["event_id"], 3, SESSION, 2, NOW,
            lambda unused_connection, unused_context, unused_event:
            {"sell_fills": 0})
        enable = self.append_phase_event(
            3, "EnableIntradayBuys", sells["event_id"])
        self.coordinator.enable_intraday_buys(
            "account-a", enable["event_id"], 4, SESSION, 3, NOW)

    def add_minute_snapshot_event(self):
        snapshot_id = "minute-fixture"
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO market_snapshots "
                "(snapshot_id, snapshot_type, stream_id, sequence_no, "
                "trading_session, decision_cutoff, status, manifest_path, "
                "manifest_sha256, created_at, committed_at, quality_codes_json) "
                "VALUES (?, 'MINUTE', ?, 1, ?, ?, 'COMMITTED', ?, ?, ?, ?, '[]')",
                (snapshot_id, "market-minute:20261009:1", SESSION, NOW,
                 "/fixture/minute.json", "b" * 64, NOW, NOW))
        event = build_domain_event(
            "MinuteSnapshotCommitted", "market-minute:20261009:1", 1,
            {"snapshot_id": snapshot_id}, occurred_at=NOW, available_at=NOW,
            source_service="test", trading_session=SESSION,
            snapshot_id=snapshot_id)
        self.events.append(event)
        return event

    def test_minute_buy_handler_cannot_run_before_phase_barrier(self):
        event = self.add_minute_snapshot_event()
        called = []
        with self.assertRaisesRegex(
                InvalidAccountSessionTransition, "not ready"):
            self.accounts.execute_intraday_buy_event(
                "account-a", event["event_id"], 1, NOW, SESSION,
                lambda unused_connection, unused_context, unused_event:
                called.append(True))
        self.assertEqual([], called)
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM processed_events WHERE event_id=?",
            (event["event_id"],)).fetchone()[0])

    def test_minute_buy_handler_runs_after_enabled_phase(self):
        self.enable_buys()
        event = self.add_minute_snapshot_event()
        called = []

        def handler(unused_connection, unused_context, unused_event):
            called.append(True)
            return AccountEventEffects(
                account_event_type="MinuteBuyEvaluated",
                account_event_payload={"fills": 0}, value={"fills": 0},
                trading_session=SESSION)

        result = self.accounts.execute_intraday_buy_event(
            "account-a", event["event_id"], 5, NOW, SESSION, handler)
        self.assertFalse(result.replayed)
        self.assertEqual([True], called)
        self.assertEqual({"fills": 0}, result.value)
        self.assertEqual(6, result.account_version)

    def test_paused_account_cannot_execute_minute_buy(self):
        self.enable_buys()
        event = self.add_minute_snapshot_event()
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE accounts SET status='PAUSED' "
                "WHERE account_id='account-a'")
        with self.assertRaisesRegex(AccountCommandRejected, "while PAUSED"):
            self.accounts.execute_intraday_buy_event(
                "account-a", event["event_id"], 5, NOW, SESSION,
                lambda unused_connection, unused_context, unused_event:
                AccountEventEffects("ShouldNotRun", {}, {}))
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM processed_events WHERE event_id=?",
            (event["event_id"],)).fetchone()[0])

    def test_minute_event_snapshot_type_mismatch_fails_closed(self):
        self.enable_buys()
        event = self.add_minute_snapshot_event()
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE market_snapshots SET snapshot_type='WATCHLIST' "
                "WHERE snapshot_id='minute-fixture'")
        with self.assertRaisesRegex(ValueError, "committed MINUTE"):
            self.accounts.execute_intraday_buy_event(
                "account-a", event["event_id"], 5, NOW, SESSION,
                lambda unused_connection, unused_context, unused_event:
                AccountEventEffects("ShouldNotRun", {}, {}))
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM processed_events WHERE event_id=?",
            (event["event_id"],)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
