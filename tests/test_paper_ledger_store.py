import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from abupy.AlphaBu.ABuIntradayExecution import (
    IntradayExecutionConfig, IntradayOrderMachine, instruction_from_order,
)
from abupy.AlphaBu.ABuTradeIntent import ApprovedOrder
from abupy.ServiceBu import (
    AccountEventEffects, DomainEventStore, InvalidLedgerTransition,
    LedgerIdentityCollision, OperationalStore, StrategyAccountStore,
    TransactionalAccountRepository, TransactionalPaperLedger,
    build_domain_event,
)
from tests.test_intraday_execution import bar


NOW = "2026-10-09T09:20:00+08:00"


class TransactionalPaperLedgerTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = OperationalStore(
            Path(self.directory.name) / "operational.sqlite3")
        registry = StrategyAccountStore(self.store)
        registry.register_strategy_instance("instance-a", "fixture", "v1", NOW)
        registry.register_strategy_instance("instance-b", "fixture", "v1", NOW)
        registry.register_account(
            "account-a", "Account A", "instance-a", NOW,
            initial_cash_micros=1_000_000_000)
        registry.register_account(
            "account-b", "Account B", "instance-b", NOW,
            initial_cash_micros=1_000_000_000)
        registry.activate_strategy(
            "activation-a", "account-a", {"version": 1}, 20261009,
            change_reason="fixture", approved_at=NOW)
        registry.activate_strategy(
            "activation-b", "account-b", {"version": 1}, 20261009,
            change_reason="fixture", approved_at=NOW)
        self.accounts = TransactionalAccountRepository(self.store)
        self.ledger = TransactionalPaperLedger()
        self.events = DomainEventStore(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def row(self, table, where, values):
        return self.store.connection.execute(
            "SELECT * FROM {} WHERE {}".format(table, where), values).fetchone()

    def approve(self, connection, context):
        self.ledger.insert_order(
            connection, context, order_id="order-a", intent_id="intent-a",
            symbol="sh600000", side="buy", quantity=100,
            status="APPROVED", valid_session=20261009,
            created_at=NOW, updated_at=NOW)
        self.ledger.insert_reservation(
            connection, context, reservation_id="reservation-a",
            order_id="order-a", reserved_cash_micros=100_100_000,
            reserved_risk_micros=10_000_000, status="ACTIVE",
            created_at=NOW, updated_at=NOW)
        self.ledger.insert_risk_decision(
            connection, context, risk_decision_id="risk-a",
            order_id="order-a", decision="APPROVE",
            reason_codes=("WITHIN_RISK_BUDGET",), policy_id="risk-v1",
            policy_version="1", source_snapshot_id=None, created_at=NOW)
        self.ledger.set_balance(
            connection, context, cash_micros=1_000_000_000,
            reserved_cash_micros=100_100_000, updated_at=NOW)

    @staticmethod
    def approved_order():
        return ApprovedOrder(
            order_id="order-a", intent_id="intent-a", strategy_id="fixture",
            strategy_version="v1", symbol="sh600000", side="buy",
            quantity=100, created_asof=20261008, valid_session=20261009,
            max_buy_price_raw=11.0, initial_stop_raw=8.0)

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

    def append_minute_event(self, sequence, previous_event_id=None):
        event = build_domain_event(
            "MinuteSnapshotCommitted", "market-minute:20261009", sequence,
            {"sequence": sequence}, previous_event_id=previous_event_id,
            occurred_at=NOW, available_at=NOW, source_service="test",
            trading_session=20261009)
        self.events.append(event)
        return event

    def test_order_reservation_risk_and_balance_map_atomically(self):
        result = self.accounts.execute("account-a", 0, NOW, self.approve)
        self.assertEqual(1, result.account_version)
        self.assertEqual("APPROVED", self.row(
            "orders", "account_id=? AND order_id=?",
            ("account-a", "order-a"))["status"])
        reservation = self.row(
            "reservations", "account_id=? AND reservation_id=?",
            ("account-a", "reservation-a"))
        self.assertEqual(100_100_000, reservation["reserved_cash_micros"])
        self.assertEqual(
            ["WITHIN_RISK_BUDGET"], json.loads(self.row(
                "risk_decisions", "risk_decision_id=?", ("risk-a",)
            )["reason_codes_json"]))
        self.assertEqual(100_100_000, self.row(
            "account_balances", "account_id=?", ("account-a",)
        )["reserved_cash_micros"])

    def test_full_fill_updates_all_projections_in_one_account_command(self):
        self.accounts.execute("account-a", 0, NOW, self.approve)

        def fill(connection, context):
            self.ledger.insert_fill(
                connection, context, fill_id="fill-a", order_id="order-a",
                symbol="sh600000", side="buy", quantity=100,
                reference_price_micros=1_000_000,
                fill_price_micros=1_001_000, fees_micros=5_000_000,
                source_snapshot_id=None,
                occurred_at="2026-10-09T09:35:02+08:00")
            self.ledger.transition_order(
                connection, context, "order-a", "FILLED", NOW)
            self.ledger.transition_reservation(
                connection, context, "reservation-a", "CONSUMED", NOW)
            self.ledger.set_balance(
                connection, context, cash_micros=894_900_000,
                reserved_cash_micros=0, updated_at=NOW)
            self.ledger.set_position(
                connection, context, symbol="sh600000", quantity=100,
                sellable_quantity=0, average_cost_micros=1_051_000,
                updated_at=NOW)

        result = self.accounts.execute("account-a", 1, NOW, fill)
        self.assertEqual(2, result.account_version)
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
            "SELECT count(*) FROM fills WHERE account_id='account-a'").fetchone()[0])

    def test_identity_collision_rolls_back_earlier_projection_changes(self):
        self.accounts.execute("account-a", 0, NOW, self.approve)

        def collision(connection, context):
            self.ledger.set_balance(
                connection, context, cash_micros=1,
                reserved_cash_micros=0, updated_at=NOW)
            self.ledger.insert_order(
                connection, context, order_id="order-a", intent_id="intent-a",
                symbol="sh600000", side="buy", quantity=200,
                status="APPROVED", valid_session=20261009,
                created_at=NOW, updated_at=NOW)

        with self.assertRaises(LedgerIdentityCollision):
            self.accounts.execute("account-a", 1, NOW, collision)
        self.assertEqual(1, self.accounts.account("account-a")["account_version"])
        self.assertEqual(1_000_000_000, self.row(
            "account_balances", "account_id=?", ("account-a",)
        )["cash_micros"])

    def test_terminal_transition_and_cross_account_reference_fail_closed(self):
        self.accounts.execute("account-a", 0, NOW, self.approve)
        self.accounts.execute(
            "account-a", 1, NOW,
            lambda connection, context: self.ledger.transition_order(
                connection, context, "order-a", "CANCELLED", NOW))
        with self.assertRaises(InvalidLedgerTransition):
            self.accounts.execute(
                "account-a", 2, NOW,
                lambda connection, context: self.ledger.transition_order(
                    connection, context, "order-a", "FILLED", NOW))
        with self.assertRaisesRegex(ValueError, "account order"):
            self.accounts.execute(
                "account-b", 0, NOW,
                lambda connection, context: self.ledger.insert_reservation(
                    connection, context, reservation_id="reservation-b",
                    order_id="order-a", reserved_cash_micros=1,
                    reserved_risk_micros=1, status="ACTIVE",
                    created_at=NOW, updated_at=NOW))
        self.assertEqual(2, self.accounts.account("account-a")["account_version"])
        self.assertEqual(0, self.accounts.account("account-b")["account_version"])

    def test_execution_machine_state_is_restored_from_committed_event(self):
        self.accounts.execute("account-a", 0, NOW, self.approve)
        order = self.approved_order()
        config = IntradayExecutionConfig(slippage_bps=0)
        instruction = instruction_from_order(order, 20261009, "M1", config)
        machine = IntradayOrderMachine(instruction, order, config=config)
        machine.on_bar(self.minute_bar("09:35"))
        event = self.append_minute_event(1)

        def persist(connection, context, unused_event):
            state = machine.export_state()
            self.ledger.stage_execution_state(
                connection, context, order_id="order-a",
                execution_policy_id="M1", state=machine.state,
                last_consumed_minute_sequence=1, machine_state=state,
                updated_at=NOW)
            return AccountEventEffects(
                account_event_type="IntradayExecutionAdvanced",
                account_event_payload={"execution_states": {"order-a": state}},
                value={"state": machine.state},
                transitioned_order_ids=("order-a",), trading_session=20261009)

        self.accounts.execute_event(
            "account-a", event["event_id"], 1, NOW, persist)
        with self.store.transaction(immediate=False) as connection:
            persisted = self.ledger.load_execution_state(
                connection, "account-a", "order-a")
        self.assertEqual(1, persisted["projection"][
            "last_consumed_minute_sequence"])
        restored = IntradayOrderMachine.restore(
            instruction, order, persisted["machine_state"], config=config)
        restored.on_bar(self.minute_bar("09:37", opening=10.2))
        self.assertEqual("FILLED", restored.state)
        self.assertEqual(3, len(restored.events))

    def test_missing_machine_state_link_rolls_back_execution_projection(self):
        self.accounts.execute("account-a", 0, NOW, self.approve)
        event = self.append_minute_event(1)

        def broken(connection, context, unused_event):
            self.ledger.stage_execution_state(
                connection, context, order_id="order-a",
                execution_policy_id="M1", state="ACTIVE",
                last_consumed_minute_sequence=1,
                machine_state={"state": "ACTIVE"}, updated_at=NOW)
            return AccountEventEffects(
                account_event_type="IntradayExecutionAdvanced",
                account_event_payload={"execution_states": {}}, value={},
                transitioned_order_ids=("order-a",), trading_session=20261009)

        with self.assertRaisesRegex(ValueError, "no persisted machine state"):
            self.accounts.execute_event(
                "account-a", event["event_id"], 1, NOW, broken)
        self.assertIsNone(self.row(
            "order_execution_states", "account_id=? AND order_id=?",
            ("account-a", "order-a")))
        self.assertEqual(1, self.accounts.account("account-a")["account_version"])


if __name__ == "__main__":
    unittest.main()
