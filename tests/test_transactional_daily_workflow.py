import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from abupy.AlphaBu.ABuTradeIntent import ApprovedOrder
from abupy.ServiceBu import (
    DomainEventStore, OperationalStore, PreopenSnapshotBuilder,
    SnapshotCatalog, StrategyAccountStore, build_domain_event,
)
from abupy.ServiceBu.ABuTransactionalDailyClose import DailyClosingMark
from abupy.ServiceBu.ABuTransactionalDailyWorkflow import (
    AccountCloseRequest, AccountOpenRequest, TransactionalAccountCloseJob,
    TransactionalAccountOpenJob, TransactionalDailyWorkflow,
)


SESSION = 20261009
NOW = "2026-10-09T09:20:00+08:00"
CLOSE = "2026-10-09T15:30:00+08:00"


class TransactionalDailyWorkflowTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = OperationalStore(
            self.root / "operational.sqlite3", target_schema_version=4)
        registry = StrategyAccountStore(self.store)
        registry.register_strategy_instance("instance-a", "fixture", "1", NOW)
        registry.register_account(
            "account-a", "Account A", "instance-a", NOW,
            initial_cash_micros=3_000_000_000)
        registry.activate_strategy(
            "activation-a", "account-a", {"version": 1}, SESSION,
            change_reason="fixture", approved_at=NOW)
        self.catalog = SnapshotCatalog(self.store, self.root / "content")
        self.events = DomainEventStore(self.store)
        self.workflow = TransactionalDailyWorkflow(
            self.store, execution_policy_id="M1")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    @staticmethod
    def order():
        return ApprovedOrder(
            order_id="order-a", intent_id="intent-a",
            strategy_id="fixture", strategy_version="1",
            symbol="sh600000", side="buy", quantity=100,
            created_asof=20261008, valid_session=SESSION,
            max_buy_price_raw=11.0, initial_stop_raw=8.0,
            position_effect="OPEN", account_id="account-a",
            strategy_instance_id="instance-a",
            actor_activation_id="activation-a")

    def append_order(self, **overrides):
        payload = {
            "approved_order": asdict(self.order()),
            "reservation_id": "reservation-a",
            "reserved_cash_micros": 1_106_000_000,
            "reserved_risk_micros": 100_000_000,
            "risk_decision_id": "risk-a",
            "upper_limit_raw": 11.0,
        }
        payload.update(overrides)
        event = build_domain_event(
            "ApprovedOrderCommitted", "approved-order:account-a:20261009", 1,
            payload, occurred_at=NOW, available_at=NOW,
            source_service="fixture", trading_session=SESSION,
            account_id="account-a")
        self.events.append(event)
        return event

    def preopen(self):
        return PreopenSnapshotBuilder(self.catalog).publish(
            trading_session=SESSION, decision_cutoff=NOW, created_at=NOW,
            security_master_version="security-v1",
            corporate_action_version="actions-v1",
            security_status_version="status-v1",
            limit_reference_version="limits-v1", receivables_cutoff=NOW,
            pending_order_snapshot_id="orders-v1")

    def open_request(self):
        snapshot, event, unused_created = self.preopen()
        return AccountOpenRequest(
            "account-a", SESSION, snapshot["snapshot_id"], event["event_id"],
            {}, NOW)

    def daily_snapshot(self):
        snapshot_id = "daily-fixture"
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO market_snapshots "
                "(snapshot_id,snapshot_type,stream_id,sequence_no,trading_session,"
                "decision_cutoff,status,manifest_path,manifest_sha256,created_at,"
                "committed_at,quality_codes_json) VALUES "
                "(?,'DAILY','market-daily:20261009',1,?,?, 'COMMITTED',?, ?,?,?, '[]')",
                (snapshot_id, SESSION, CLOSE, "/fixture/daily.json",
                 "d" * 64, CLOSE, CLOSE))
        return snapshot_id

    def test_open_registers_frozen_order_and_reaches_intraday_barrier_once(self):
        order_event = self.append_order()
        request = self.open_request()
        first = self.workflow.open_day(request)
        version = first["account_version"]
        replay = self.workflow.open_day(request)
        self.assertEqual("INTRADAY_BUYS_ENABLED", first["status"])
        self.assertEqual(["order-a"], [item["order_id"]
                                      for item in first["registered_orders"]])
        self.assertEqual(version, replay["account_version"])
        self.assertEqual([], replay["registered_orders"])
        self.assertEqual("APPROVED", self.store.connection.execute(
            "SELECT status FROM orders WHERE order_id='order-a'").fetchone()[0])
        self.assertEqual(1, self.store.connection.execute(
            "SELECT count(*) FROM processed_events WHERE account_id='account-a' "
            "AND event_id=?", (order_event["event_id"],)).fetchone()[0])

    def test_incomplete_risk_reservation_fails_closed_before_preopen(self):
        self.append_order(reservation_id=None)
        request = self.open_request()
        with self.assertRaisesRegex(ValueError, "reservation"):
            self.workflow.open_day(request)
        row = self.store.connection.execute(
            "SELECT phase FROM account_sessions WHERE account_id='account-a' "
            "AND trading_session=?", (SESSION,)).fetchone()
        self.assertEqual("CREATED", row["phase"])
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM orders").fetchone()[0])

    def test_close_reconciles_and_is_idempotent(self):
        self.append_order()
        self.workflow.open_day(self.open_request())
        snapshot_id = self.daily_snapshot()
        request = AccountCloseRequest(
            "account-a", SESSION, snapshot_id, {}, CLOSE)
        first = self.workflow.close_day(request)
        version = first["account_version"]
        replay = self.workflow.close_day(request)
        self.assertEqual("PASSED", first["reconciliation_status"])
        self.assertEqual(version, replay["account_version"])
        self.assertEqual(1, self.store.connection.execute(
            "SELECT count(*) FROM account_daily_closes").fetchone()[0])

    def test_batch_jobs_report_pinned_outputs(self):
        self.append_order()
        open_request = self.open_request()
        opened = TransactionalAccountOpenJob(
            self.workflow, lambda: [open_request])()
        snapshot_id = self.daily_snapshot()
        closed = TransactionalAccountCloseJob(
            self.workflow, lambda: [AccountCloseRequest(
                "account-a", SESSION, snapshot_id, {}, CLOSE)])()
        self.assertEqual("ACCOUNT_OPEN_BATCH_COMPLETED", opened["status"])
        self.assertEqual([open_request.preopen_snapshot_id],
                         opened["output_snapshot_ids"])
        self.assertEqual("ACCOUNT_CLOSE_BATCH_COMPLETED", closed["status"])
        self.assertEqual([snapshot_id], closed["output_snapshot_ids"])

    def test_closing_marks_require_committed_daily_snapshot(self):
        self.append_order()
        self.workflow.open_day(self.open_request())
        marks = {"sh600000": DailyClosingMark(
            "sh600000", 10.0, CLOSE, "raw-price-v1")}
        with self.assertRaisesRegex(ValueError, "committed daily"):
            self.workflow.close_day(AccountCloseRequest(
                "account-a", SESSION, "missing", marks, CLOSE))


if __name__ == "__main__":
    unittest.main()
