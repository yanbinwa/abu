import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu import (
    AccountSessionCoordinator, AccountSessionStore, DomainEventStore,
    OperationalStore, PreopenSnapshotBuilder, SnapshotCatalog,
    StrategyAccountStore, TransactionalAccountRepository,
    TransactionalPaperLedger, build_domain_event,
)
from abupy.ServiceBu.ABuTransactionalDailyClose import (
    AccountReconciliationError, DailyClosingMark, TransactionalDailyCloser,
)


NOW = "2026-10-12T15:30:00+08:00"
SESSION = 20261012


class TransactionalDailyCloseTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = OperationalStore(
            root / "operational.sqlite3", target_schema_version=3)
        registry = StrategyAccountStore(self.store)
        registry.register_strategy_instance("instance-a", "fixture", "v1", NOW)
        registry.register_account(
            "account-a", "Account A", "instance-a", NOW,
            initial_cash_micros=2_000_000_000)
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
        self.ledger = TransactionalPaperLedger()
        self.accounts.execute("account-a", 0, NOW, self._seed_account_day)
        self._advance_to_intraday_enabled()
        self.closer = TransactionalDailyCloser(
            self.accounts, self.coordinator)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def _seed_account_day(self, connection, context):
        self.ledger.insert_logical_trade(
            connection, context, trade_id="trade-a", symbol="sh600000",
            opened_under_activation_id="activation-a",
            management_activation_id="activation-a",
            entry_policy_id="fixture-entry", entry_policy_version="1",
            exit_policy_id="fixture-exit", exit_policy_version="1",
            risk_policy_id="fixture-risk", risk_policy_version="1",
            opened_at=NOW)
        self.ledger.insert_position_lot(
            connection, context, lot_id="lot-a", trade_id="trade-a",
            symbol="sh600000", quantity=100,
            sellable_on_session=SESSION,
            cost_price_micros=10_000_000, opened_at=NOW)
        self.ledger.set_position(
            connection, context, symbol="sh600000", quantity=100,
            sellable_quantity=100, average_cost_micros=10_000_000,
            updated_at=NOW)
        return self.sessions.create(connection, context, SESSION, NOW)

    def append_phase_event(self, sequence, event_type, previous_event_id=None):
        event = build_domain_event(
            event_type, "account-phase:account-a:20261012", sequence,
            {"phase_command": event_type},
            previous_event_id=previous_event_id, occurred_at=NOW,
            available_at=NOW, source_service="test",
            trading_session=SESSION, account_id="account-a")
        self.events.append(event)
        return event

    def _advance_to_intraday_enabled(self):
        snapshot, event, unused_created = self.preopen.publish(
            trading_session=SESSION, decision_cutoff=NOW, created_at=NOW,
            security_master_version="security-v1",
            corporate_action_version="actions-v1",
            security_status_version="status-v1",
            limit_reference_version="limits-v1", receivables_cutoff=NOW,
            pending_order_snapshot_id="orders-v1")
        self.coordinator.accept_preopen_inputs(
            "account-a", event["event_id"], 1, SESSION, 0,
            snapshot["snapshot_id"], NOW)
        receivables = self.append_phase_event(
            1, "ApplyReceivablesAndCorporateActions")
        self.coordinator.apply_receivables_and_corporate_actions(
            "account-a", receivables["event_id"], 2, SESSION, 1, NOW,
            lambda unused_connection, unused_context, unused_event: {})
        sells = self.append_phase_event(
            2, "ProcessOpenSells", receivables["event_id"])
        self.coordinator.process_open_sells(
            "account-a", sells["event_id"], 3, SESSION, 2, NOW,
            lambda unused_connection, unused_context, unused_event: {})
        enable = self.append_phase_event(
            3, "EnableIntradayBuys", sells["event_id"])
        self.coordinator.enable_intraday_buys(
            "account-a", enable["event_id"], 4, SESSION, 3, NOW)

    def marks(self):
        return {"sh600000": DailyClosingMark(
            "sh600000", 12.0, NOW, "raw-price-v1")}

    def closing_event(self, marks, *, stream="account-close-input:a", sequence=1,
                      previous_event_id=None):
        snapshot_id = "daily-close-fixture-{}".format(sequence)
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO market_snapshots "
                "(snapshot_id, snapshot_type, stream_id, sequence_no, "
                "trading_session, decision_cutoff, status, manifest_path, "
                "manifest_sha256, created_at, committed_at, quality_codes_json) "
                "VALUES (?, 'DAILY', ?, 1, ?, ?, 'COMMITTED', ?, ?, ?, ?, '[]')",
                (snapshot_id, "market-daily:fixture:{}".format(sequence),
                 SESSION, NOW, "/fixture/{}.json".format(snapshot_id),
                 "d" * 64, NOW, NOW))
        event = build_domain_event(
            "DailyClosingMarksPrepared", stream, sequence, {
                "source_snapshot_id": snapshot_id,
                "closing_marks_sha256": self.closer.marks_sha256(marks),
            }, previous_event_id=previous_event_id, occurred_at=NOW,
            available_at=NOW, source_service="test", trading_session=SESSION,
            snapshot_id=snapshot_id, account_id="account-a")
        self.events.append(event)
        return event

    def session_row(self):
        return self.store.connection.execute(
            "SELECT * FROM account_sessions WHERE account_id='account-a' "
            "AND trading_session=?", (SESSION,)).fetchone()

    def test_daily_close_values_account_and_replays_once(self):
        marks = self.marks()
        event = self.closing_event(marks)
        result = self.closer.complete_daily_close(
            "account-a", event["event_id"], 5, SESSION, 4, marks, NOW)
        replay = self.closer.complete_daily_close(
            "account-a", event["event_id"], 5, SESSION, 4, marks, NOW)
        self.assertTrue(replay.replayed)
        close = self.store.connection.execute(
            "SELECT * FROM account_daily_closes WHERE account_id='account-a' "
            "AND trading_session=?", (SESSION,)).fetchone()
        self.assertEqual(1_200_000_000, close["market_value_micros"])
        self.assertEqual(1_000_000_000, close["position_cost_micros"])
        self.assertEqual(200_000_000, close["unrealized_pnl_micros"])
        self.assertEqual(3_200_000_000, close["equity_micros"])
        self.assertEqual("DAILY_CLOSE_COMPLETED", self.session_row()["phase"])
        self.assertEqual("PASSED", self.store.connection.execute(
            "SELECT status FROM reconciliation_runs WHERE account_id='account-a'"
        ).fetchone()[0])
        self.assertEqual("PASSED",
                         result.value["domain_value"]["reconciliation_status"])

    def test_missing_mark_records_failure_without_advancing_account(self):
        marks = {}
        event = self.closing_event(marks)
        with self.assertRaisesRegex(
                AccountReconciliationError, "MISSING_CLOSING_MARK"):
            self.closer.complete_daily_close(
                "account-a", event["event_id"], 5, SESSION, 4, marks, NOW)
        self.assertEqual("INTRADAY_BUYS_ENABLED", self.session_row()["phase"])
        self.assertEqual(5, self.accounts.account("account-a")["account_version"])
        self.assertEqual("FAILED", self.store.connection.execute(
            "SELECT status FROM reconciliation_runs WHERE account_id='account-a'"
        ).fetchone()[0])
        self.assertEqual("CRITICAL", self.store.connection.execute(
            "SELECT severity FROM audit_findings "
            "WHERE category='ACCOUNT_DAILY_RECONCILIATION'"
        ).fetchone()[0])
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM processed_events WHERE event_id=?",
            (event["event_id"],)).fetchone()[0])

    def test_lot_mismatch_fails_before_daily_close_fact(self):
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE position_lots SET quantity=90 WHERE lot_id='lot-a'")
        marks = self.marks()
        event = self.closing_event(marks)
        with self.assertRaisesRegex(
                AccountReconciliationError, "POSITION_LOT_QUANTITY_MISMATCH"):
            self.closer.complete_daily_close(
                "account-a", event["event_id"], 5, SESSION, 4, marks, NOW)
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM account_daily_closes").fetchone()[0])
        self.assertEqual("INTRADAY_BUYS_ENABLED", self.session_row()["phase"])
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE position_lots SET quantity=100 WHERE lot_id='lot-a'")
        recovered = self.closer.complete_daily_close(
            "account-a", event["event_id"], 5, SESSION, 4, marks, NOW)
        self.assertEqual("PASSED",
                         recovered.value["domain_value"]["reconciliation_status"])
        finding = self.store.connection.execute(
            "SELECT resolved_at FROM audit_findings "
            "WHERE category='ACCOUNT_DAILY_RECONCILIATION'"
        ).fetchone()
        self.assertEqual(NOW, finding["resolved_at"])

    def test_unpinned_marks_are_rejected_without_reconciliation_record(self):
        marks = self.marks()
        event = self.closing_event(marks)
        changed = {"sh600000": DailyClosingMark(
            "sh600000", 13.0, NOW, "raw-price-v1")}
        with self.assertRaisesRegex(ValueError, "not pinned"):
            self.closer.complete_daily_close(
                "account-a", event["event_id"], 5, SESSION, 4, changed, NOW)
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM reconciliation_runs").fetchone()[0])
        self.assertEqual("INTRADAY_BUYS_ENABLED", self.session_row()["phase"])


if __name__ == "__main__":
    unittest.main()
