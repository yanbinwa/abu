import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from abupy.AlphaBu.ABuIntradayExecution import IntradayExecutionConfig
from abupy.AlphaBu.ABuTradeIntent import ApprovedOrder
from abupy.ServiceBu import (
    AccountSessionCoordinator, AccountSessionStore, DomainEventStore,
    MinuteSnapshotBatch, OperationalStore, PreopenSnapshotBuilder,
    SnapshotCatalog, StrategyAccountStore, TransactionalAccountRepository,
    TransactionalPaperLedger, build_domain_event,
)
from abupy.ServiceBu.ABuTransactionalIntradayBroker import (
    TransactionalIntradayBroker,
)
from tests.test_intraday_execution import bar


NOW = "2026-10-09T09:20:00+08:00"
SESSION = 20261009


class FailingPositionLedger(TransactionalPaperLedger):

    @staticmethod
    def set_position(*unused_args, **unused_kwargs):
        raise RuntimeError("fault after fill before position")


class TransactionalIntradayBrokerTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = OperationalStore(root / "operational.sqlite3")
        registry = StrategyAccountStore(self.store)
        registry.register_strategy_instance("instance-a", "fixture", "v1", NOW)
        registry.register_account(
            "account-a", "Account A", "instance-a", NOW,
            initial_cash_micros=3_000_000_000)
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
        self.config = IntradayExecutionConfig(slippage_bps=0)
        self.broker = TransactionalIntradayBroker(
            self.accounts, execution_policy_id="M1",
            intraday_config=self.config)
        self.accounts.execute(
            "account-a", 0, NOW,
            lambda connection, context: self.sessions.create(
                connection, context, SESSION, NOW))
        self.order_event = self.append_event(
            "ApprovedOrderCommitted", "approved-order:account-a:20261009", 1,
            payload={"approved_order": asdict(self.approved_order())})

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def approved_order(self):
        return ApprovedOrder(
            order_id="order-a", intent_id="intent-a",
            strategy_id="fixture", strategy_version="v1",
            symbol="sh600000", side="buy", quantity=100,
            created_asof=20261008, valid_session=SESSION,
            max_buy_price_raw=11.0, initial_stop_raw=8.0,
            position_effect="OPEN", account_id="account-a",
            strategy_instance_id="instance-a",
            actor_activation_id="activation-a")

    def append_event(self, event_type, stream_id, sequence,
                     previous_event_id=None, snapshot_id=None, payload=None):
        event = build_domain_event(
            event_type, stream_id, sequence,
            payload or {"event_type": event_type, "snapshot_id": snapshot_id},
            previous_event_id=previous_event_id, occurred_at=NOW,
            available_at=NOW, source_service="test", trading_session=SESSION,
            snapshot_id=snapshot_id, account_id=(
                "account-a" if stream_id.startswith("approved-order") or
                stream_id.startswith("account-phase") else None))
        self.events.append(event)
        return event

    def register_order(self, broker=None):
        broker = broker or self.broker
        return broker.register_approved_order(
            "account-a", self.order_event["event_id"], 1,
            self.approved_order(), reservation_id="reservation-a",
            reserved_cash_micros=1_106_000_000,
            reserved_risk_micros=100_000_000,
            risk_decision_id="risk-a", processed_at=NOW,
            upper_limit_raw=11.0)

    def enable_buys(self):
        snapshot, event, unused_created = self.preopen.publish(
            trading_session=SESSION, decision_cutoff=NOW, created_at=NOW,
            security_master_version="security-v1",
            corporate_action_version="actions-v1",
            security_status_version="status-v1",
            limit_reference_version="limits-v1", receivables_cutoff=NOW,
            pending_order_snapshot_id="orders-v1")
        self.coordinator.accept_preopen_inputs(
            "account-a", event["event_id"], 2, SESSION, 0,
            snapshot["snapshot_id"], NOW)
        first = self.append_event(
            "ApplyReceivables", "account-phase:account-a:20261009", 1)
        self.coordinator.apply_receivables_and_corporate_actions(
            "account-a", first["event_id"], 3, SESSION, 1, NOW,
            lambda unused_connection, unused_context, unused_event: {})
        second = self.append_event(
            "ProcessOpenSells", first["stream_id"], 2,
            previous_event_id=first["event_id"])
        self.coordinator.process_open_sells(
            "account-a", second["event_id"], 4, SESSION, 2, NOW,
            lambda unused_connection, unused_context, unused_event: {})
        third = self.append_event(
            "EnableIntradayBuys", first["stream_id"], 3,
            previous_event_id=second["event_id"])
        self.coordinator.enable_intraday_buys(
            "account-a", third["event_id"], 5, SESSION, 3, NOW)

    @staticmethod
    def minute_bar(end, **kwargs):
        item = bar(end, **kwargs)
        return replace(
            item, symbol="sh600000",
            source_timestamp=item.source_timestamp.replace(
                "2025-01-03", "2026-10-09"),
            bar_start=item.bar_start.replace("2025-01-03", "2026-10-09"),
            bar_end=item.bar_end.replace("2025-01-03", "2026-10-09"),
            request_started_at=item.request_started_at.replace(
                "2025-01-03", "2026-10-09"),
            received_at=item.received_at.replace(
                "2025-01-03", "2026-10-09"),
            available_at=item.available_at.replace(
                "2025-01-03", "2026-10-09"))

    def minute_snapshot(self, sequence, events, previous=None,
                        previous_event_id=None, decision_cutoff=None):
        snapshot_id = "minute-{}".format(sequence)
        stream_id = "market-minute:20261009:1"
        cutoff = decision_cutoff or events[-1].available_at
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO market_snapshots "
                "(snapshot_id, snapshot_type, stream_id, sequence_no, "
                "previous_snapshot_id, trading_session, decision_cutoff, status, "
                "manifest_path, manifest_sha256, created_at, committed_at, "
                "quality_codes_json) "
                "VALUES (?, 'MINUTE', ?, ?, ?, ?, ?, 'COMMITTED', ?, ?, ?, ?, '[]')",
                (snapshot_id, stream_id, sequence, previous, SESSION, cutoff,
                 "/fixture/{}.json".format(snapshot_id), "b" * 64, NOW, NOW))
        event = self.append_event(
            "MinuteSnapshotCommitted", stream_id, sequence,
            previous_event_id=previous_event_id, snapshot_id=snapshot_id)
        batch = MinuteSnapshotBatch(
            snapshot_id=snapshot_id, stream_id=stream_id,
            sequence_no=sequence, previous_snapshot_id=previous,
            decision_cutoff=cutoff, watchlist_id="watchlist-a",
            watchlist_version=1, manifest={},
            events_by_symbol={"sh600000": tuple(events)})
        return event, batch

    def row(self, table, where, values):
        return self.store.connection.execute(
            "SELECT * FROM {} WHERE {}".format(table, where), values).fetchone()

    def test_registration_persists_self_contained_active_machine(self):
        result = self.register_order()
        self.assertEqual("ACTIVE", result.value["state"])
        state = self.row(
            "order_execution_states", "account_id=? AND order_id=?",
            ("account-a", "order-a"))
        self.assertEqual(0, state["last_consumed_minute_sequence"])
        self.assertIsNotNone(state["last_transition_event_id"])
        persisted = TransactionalPaperLedger.load_execution_state(
            self.store.connection, "account-a", "order-a")
        self.assertEqual("intraday_order_machine_state_v2",
                         persisted["machine_state"]["schema_version"])
        self.assertEqual("order-a", persisted["machine_state"]["order"]["order_id"])
        self.assertEqual(1_106_000_000, self.row(
            "account_balances", "account_id=?", ("account-a",)
        )["reserved_cash_micros"])

    def test_registration_rejects_event_payload_order_mismatch(self):
        wrong = replace(self.approved_order(), quantity=200)
        with self.assertRaisesRegex(ValueError, "payload mismatch"):
            self.broker.register_approved_order(
                "account-a", self.order_event["event_id"], 1, wrong,
                reservation_id="reservation-a",
                reserved_cash_micros=2_212_000_000,
                reserved_risk_micros=100_000_000,
                risk_decision_id="risk-a", processed_at=NOW,
                upper_limit_raw=11.0)
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM orders").fetchone()[0])
        self.assertEqual(1, self.accounts.account("account-a")["account_version"])

    def test_restart_resumes_next_snapshot_and_commits_fill_once(self):
        self.register_order()
        self.enable_buys()
        first_event, first_batch = self.minute_snapshot(
            1, (self.minute_bar("09:35"),))
        first = self.broker.process_snapshot_batch(
            "account-a", first_event["event_id"], 6, SESSION,
            first_batch, NOW)
        self.assertEqual("CANDIDATE", first.value["outcomes"][0]["state"])
        restarted = TransactionalIntradayBroker(
            self.accounts, execution_policy_id="M1",
            intraday_config=self.config)
        second_event, second_batch = self.minute_snapshot(
            2, (self.minute_bar("09:37", opening=10.2),),
            previous=first_batch.snapshot_id,
            previous_event_id=first_event["event_id"])
        second = restarted.process_snapshot_batch(
            "account-a", second_event["event_id"], 7, SESSION,
            second_batch, NOW)
        replay = restarted.process_snapshot_batch(
            "account-a", second_event["event_id"], 7, SESSION,
            second_batch, NOW)
        self.assertEqual("FILLED", second.value["outcomes"][0]["state"])
        self.assertTrue(replay.replayed)
        self.assertEqual(1, self.store.connection.execute(
            "SELECT count(*) FROM fills WHERE account_id='account-a'").fetchone()[0])
        self.assertEqual("FILLED", self.row(
            "orders", "account_id=? AND order_id=?",
            ("account-a", "order-a"))["status"])
        self.assertEqual("CONSUMED", self.row(
            "reservations", "account_id=? AND reservation_id=?",
            ("account-a", "reservation-a"))["status"])
        self.assertEqual(100, self.row(
            "positions", "account_id=? AND symbol=?",
            ("account-a", "sh600000"))["quantity"])
        self.assertEqual(1, self.store.connection.execute(
            "SELECT count(*) FROM notification_outbox").fetchone()[0])
        self.assertEqual(2, self.store.connection.execute(
            "SELECT count(*) FROM notification_parts").fetchone()[0])

    def test_failure_after_fill_rolls_back_every_projection(self):
        self.register_order()
        self.enable_buys()
        first_event, first_batch = self.minute_snapshot(
            1, (self.minute_bar("09:35"),))
        self.broker.process_snapshot_batch(
            "account-a", first_event["event_id"], 6, SESSION,
            first_batch, NOW)
        second_event, second_batch = self.minute_snapshot(
            2, (self.minute_bar("09:37", opening=10.2),),
            previous=first_batch.snapshot_id,
            previous_event_id=first_event["event_id"])
        failing = TransactionalIntradayBroker(
            self.accounts, execution_policy_id="M1",
            intraday_config=self.config, ledger=FailingPositionLedger())
        with self.assertRaisesRegex(RuntimeError, "fault after fill"):
            failing.process_snapshot_batch(
                "account-a", second_event["event_id"], 7, SESSION,
                second_batch, NOW)
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM fills").fetchone()[0])
        self.assertEqual("WAITING", self.row(
            "orders", "account_id=? AND order_id=?",
            ("account-a", "order-a"))["status"])
        state = self.row(
            "order_execution_states", "account_id=? AND order_id=?",
            ("account-a", "order-a"))
        self.assertEqual("CANDIDATE", state["state"])
        self.assertEqual(1, state["last_consumed_minute_sequence"])
        self.assertEqual(7, self.accounts.account("account-a")["account_version"])
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM processed_events WHERE event_id=?",
            (second_event["event_id"],)).fetchone()[0])

    def test_window_expiry_releases_reservation(self):
        self.register_order()
        self.enable_buys()
        event, batch = self.minute_snapshot(
            1, (self.minute_bar("10:32"),),
            decision_cutoff="2026-10-09T10:32:02+08:00")
        result = self.broker.process_snapshot_batch(
            "account-a", event["event_id"], 6, SESSION, batch, NOW)
        self.assertEqual("EXPIRED", result.value["outcomes"][0]["state"])
        self.assertEqual("EXPIRED", self.row(
            "orders", "account_id=? AND order_id=?",
            ("account-a", "order-a"))["status"])
        self.assertEqual(0, self.row(
            "account_balances", "account_id=?", ("account-a",)
        )["reserved_cash_micros"])

    def test_m2_waits_for_capacity_then_fills_from_later_reference(self):
        m2 = TransactionalIntradayBroker(
            self.accounts, execution_policy_id="M2",
            intraday_config=self.config)
        self.register_order(m2)
        self.enable_buys()
        first_event, first_batch = self.minute_snapshot(
            1, (self.minute_bar("09:35", volume=1_000),))
        first = m2.process_snapshot_batch(
            "account-a", first_event["event_id"], 6, SESSION,
            first_batch, NOW)
        self.assertEqual("ACTIVE", first.value["outcomes"][0]["state"])
        second_event, second_batch = self.minute_snapshot(
            2, (self.minute_bar("09:36", volume=5_000),),
            previous=first_batch.snapshot_id,
            previous_event_id=first_event["event_id"])
        second = m2.process_snapshot_batch(
            "account-a", second_event["event_id"], 7, SESSION,
            second_batch, NOW)
        self.assertEqual("CANDIDATE", second.value["outcomes"][0]["state"])
        third_event, third_batch = self.minute_snapshot(
            3, (self.minute_bar("09:38", opening=10.2),),
            previous=second_batch.snapshot_id,
            previous_event_id=second_event["event_id"])
        third = m2.process_snapshot_batch(
            "account-a", third_event["event_id"], 8, SESSION,
            third_batch, NOW)
        self.assertEqual("FILLED", third.value["outcomes"][0]["state"])
        self.assertEqual(1, len(third.value["fills"]))


if __name__ == "__main__":
    unittest.main()
