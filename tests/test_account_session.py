import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu import (
    AccountSessionStore, AccountSessionVersionConflict,
    InvalidAccountSessionTransition, OperationalStore, StrategyAccountStore,
    PreopenSnapshotBuilder, PreopenSnapshotNotReady, SnapshotCatalog,
    TransactionalAccountRepository,
)


NOW = "2026-10-09T08:30:00+08:00"
SESSION = 20261009


class AccountSessionStoreTest(unittest.TestCase):

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
        self.preopen = PreopenSnapshotBuilder(
            SnapshotCatalog(self.store, root / "content"))

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def session(self):
        row = self.store.connection.execute(
            "SELECT * FROM account_sessions "
            "WHERE account_id='account-a' AND trading_session=?", (SESSION,)
        ).fetchone()
        return None if row is None else dict(row)

    def add_preopen_snapshot(self, **overrides):
        values = {
            "trading_session": SESSION,
            "decision_cutoff": NOW,
            "created_at": NOW,
            "security_master_version": "security-v1",
            "corporate_action_version": "actions-v1",
            "security_status_version": "status-v1",
            "limit_reference_version": "limits-v1",
            "receivables_cutoff": NOW,
            "pending_order_snapshot_id": "orders-v1",
        }
        values.update(overrides)
        snapshot, unused_event, unused_created = self.preopen.publish(**values)
        return snapshot["snapshot_id"]

    def create_session(self):
        return self.accounts.execute(
            "account-a", 0, NOW,
            lambda connection, context: self.sessions.create(
                connection, context, SESSION, NOW))

    def test_create_is_atomic_with_account_version(self):
        result = self.create_session()
        row, created = result.value
        self.assertTrue(created)
        self.assertEqual("CREATED", row["phase"])
        self.assertEqual(1, result.account_version)
        self.assertEqual(0, self.session()["phase_version"])

    def test_create_requires_activation_covering_session(self):
        with self.assertRaisesRegex(ValueError, "does not cover"):
            self.accounts.execute(
                "account-a", 0, NOW,
                lambda connection, context: self.sessions.create(
                    connection, context, 20261008, NOW))
        self.assertIsNone(self.session())
        self.assertEqual(0, self.accounts.account("account-a")["account_version"])

    def test_only_adjacent_phase_transition_is_allowed(self):
        self.create_session()
        with self.assertRaisesRegex(
                InvalidAccountSessionTransition, "cannot transition"):
            self.accounts.execute(
                "account-a", 1, NOW,
                lambda connection, context: self.sessions.transition(
                    connection, context, SESSION, "RECEIVABLES_APPLIED", 0,
                    NOW))
        self.assertEqual("CREATED", self.session()["phase"])
        self.assertEqual(1, self.accounts.account("account-a")["account_version"])

    def test_preopen_transition_requires_matching_committed_snapshot(self):
        self.create_session()
        with self.assertRaisesRegex(ValueError, "preopen_snapshot_id"):
            self.accounts.execute(
                "account-a", 1, NOW,
                lambda connection, context: self.sessions.transition(
                    connection, context, SESSION, "PREOPEN_INPUTS_READY", 0,
                    NOW))
        snapshot_id = self.add_preopen_snapshot()
        result = self.accounts.execute(
            "account-a", 1, NOW,
            lambda connection, context: self.sessions.transition(
                connection, context, SESSION, "PREOPEN_INPUTS_READY", 0,
                NOW, preopen_snapshot_id=snapshot_id))
        self.assertTrue(result.value.changed)
        self.assertEqual(snapshot_id, self.session()["preopen_snapshot_id"])
        self.assertEqual(1, self.session()["phase_version"])

    def test_preopen_transition_rejects_missing_required_input(self):
        self.create_session()
        snapshot_id = self.add_preopen_snapshot(
            corporate_action_version=None)
        with self.assertRaisesRegex(
                PreopenSnapshotNotReady, "corporate_actions"):
            self.accounts.execute(
                "account-a", 1, NOW,
                lambda connection, context: self.sessions.transition(
                    connection, context, SESSION, "PREOPEN_INPUTS_READY", 0,
                    NOW, preopen_snapshot_id=snapshot_id))
        self.assertEqual("CREATED", self.session()["phase"])
        self.assertEqual(1, self.accounts.account("account-a")["account_version"])

    def test_stale_phase_version_rolls_back_account_version(self):
        self.create_session()
        snapshot_id = self.add_preopen_snapshot()
        self.accounts.execute(
            "account-a", 1, NOW,
            lambda connection, context: self.sessions.transition(
                connection, context, SESSION, "PREOPEN_INPUTS_READY", 0,
                NOW, preopen_snapshot_id=snapshot_id))
        with self.assertRaisesRegex(
                AccountSessionVersionConflict, "expected phase version"):
            self.accounts.execute(
                "account-a", 2, NOW,
                lambda connection, context: self.sessions.transition(
                    connection, context, SESSION, "RECEIVABLES_APPLIED", 0,
                    NOW))
        self.assertEqual(2, self.accounts.account("account-a")["account_version"])
        self.assertEqual("PREOPEN_INPUTS_READY", self.session()["phase"])

    def test_block_requires_explicit_unblock_before_progress(self):
        self.create_session()
        self.accounts.execute(
            "account-a", 1, NOW,
            lambda connection, context: self.sessions.block(
                connection, context, SESSION, 0, "PREOPEN_SOURCE_MISSING", NOW))
        self.assertEqual("PREOPEN_SOURCE_MISSING", self.session()["blocked_reason"])
        snapshot_id = self.add_preopen_snapshot()
        with self.assertRaisesRegex(
                InvalidAccountSessionTransition, "explicitly unblocked"):
            self.accounts.execute(
                "account-a", 2, NOW,
                lambda connection, context: self.sessions.transition(
                    connection, context, SESSION, "PREOPEN_INPUTS_READY", 1,
                    NOW, preopen_snapshot_id=snapshot_id))
        self.accounts.execute(
            "account-a", 2, NOW,
            lambda connection, context: self.sessions.unblock(
                connection, context, SESSION, 1, NOW))
        self.accounts.execute(
            "account-a", 3, NOW,
            lambda connection, context: self.sessions.transition(
                connection, context, SESSION, "PREOPEN_INPUTS_READY", 2,
                NOW, preopen_snapshot_id=snapshot_id))
        self.assertEqual("PREOPEN_INPUTS_READY", self.session()["phase"])
        self.assertIsNone(self.session()["blocked_reason"])

    def test_required_phase_rejects_early_or_blocked_session(self):
        self.create_session()
        with self.store.transaction(immediate=False) as connection:
            with self.assertRaisesRegex(
                    InvalidAccountSessionTransition, "not ready"):
                self.sessions.require_phase(
                    connection, "account-a", SESSION,
                    "INTRADAY_BUYS_ENABLED")

    def test_failure_after_phase_update_rolls_back_phase_and_account(self):
        self.create_session()
        snapshot_id = self.add_preopen_snapshot()

        def broken(connection, context):
            self.sessions.transition(
                connection, context, SESSION, "PREOPEN_INPUTS_READY", 0,
                NOW, preopen_snapshot_id=snapshot_id)
            raise RuntimeError("fault after phase update")

        with self.assertRaisesRegex(RuntimeError, "fault after"):
            self.accounts.execute("account-a", 1, NOW, broken)
        self.assertEqual("CREATED", self.session()["phase"])
        self.assertEqual(0, self.session()["phase_version"])
        self.assertEqual(1, self.accounts.account("account-a")["account_version"])


if __name__ == "__main__":
    unittest.main()
