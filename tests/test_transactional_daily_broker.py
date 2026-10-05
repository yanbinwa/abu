import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from abupy.AlphaBu.ABuTradeIntent import ApprovedOrder
from abupy.ServiceBu import (
    AccountSessionCoordinator, AccountSessionStore, DomainEventStore,
    OperationalStore, PreopenSnapshotBuilder, SnapshotCatalog,
    StrategyAccountStore, TransactionalAccountRepository,
    TransactionalPaperLedger, build_domain_event,
)
from abupy.ServiceBu.ABuTransactionalDailyBroker import (
    AccountReceivableInstruction, DailyOpenQuote, TransactionalDailyBroker,
)


NOW = "2026-10-09T15:10:00+08:00"
NEXT_NOW = "2026-10-12T09:20:00+08:00"
SESSION = 20261009
NEXT_SESSION = 20261012
TRADE_ID = "trade-a"
LOT_ID = "lot-a"


class FailAfterSellLedger(TransactionalPaperLedger):

    @classmethod
    def transition_order(cls, connection, context, order_id, status, updated_at):
        if status == "FILLED":
            raise RuntimeError("fault after sell facts")
        return super(FailAfterSellLedger, cls).transition_order(
            connection, context, order_id, status, updated_at)


class TransactionalDailyBrokerTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = OperationalStore(
            root / "operational.sqlite3", target_schema_version=2)
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
        self.accounts.execute("account-a", 0, NOW, self._seed_position)
        self.accounts.execute(
            "account-a", 1, NOW,
            lambda connection, context: self.sessions.create(
                connection, context, NEXT_SESSION, NOW))
        self.broker = TransactionalDailyBroker(
            self.accounts, self.coordinator, slippage_bps=0)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def _seed_position(self, connection, context):
        self.ledger.insert_logical_trade(
            connection, context, trade_id=TRADE_ID, symbol="sh600000",
            opened_under_activation_id="activation-a",
            management_activation_id="activation-a",
            entry_policy_id="fixture-entry", entry_policy_version="1",
            exit_policy_id="fixture-exit", exit_policy_version="1",
            risk_policy_id="fixture-risk", risk_policy_version="1",
            opened_at=NOW)
        self.ledger.insert_position_lot(
            connection, context, lot_id=LOT_ID, trade_id=TRADE_ID,
            symbol="sh600000", quantity=100,
            sellable_on_session=NEXT_SESSION,
            cost_price_micros=10_000_000, opened_at=NOW)
        self.ledger.set_position(
            connection, context, symbol="sh600000", quantity=100,
            sellable_quantity=0, average_cost_micros=10_000_000,
            updated_at=NOW)

    def account_version(self):
        return self.accounts.account("account-a")["account_version"]

    def row(self, table, where, values):
        return self.store.connection.execute(
            "SELECT * FROM {} WHERE {}".format(table, where), values).fetchone()

    def append_event(self, event_type, stream_id, payload, *, sequence=1,
                     session=NEXT_SESSION, previous_event_id=None):
        event = build_domain_event(
            event_type, stream_id, sequence, payload,
            previous_event_id=previous_event_id, occurred_at=NEXT_NOW,
            available_at=NEXT_NOW, source_service="test",
            trading_session=session, account_id="account-a")
        self.events.append(event)
        return event

    def sell_order(self, *, valid_session=NEXT_SESSION):
        return ApprovedOrder(
            order_id="sell-a", intent_id="sell-intent-a",
            strategy_id="fixture", strategy_version="v1",
            symbol="sh600000", side="sell", quantity=100,
            created_asof=SESSION, valid_session=valid_session,
            reason="daily exit", target_trade_id=TRADE_ID,
            position_effect="CLOSE", account_id="account-a",
            strategy_instance_id="instance-a",
            actor_activation_id="activation-a")

    def register_sell(self, broker=None):
        broker = broker or self.broker
        order = self.sell_order()
        event = self.append_event(
            "ApprovedOrderCommitted", "approved-sell:account-a:20261012",
            {"approved_order": asdict(order)})
        result = broker.register_sell_order(
            "account-a", event["event_id"], self.account_version(), order,
            reservation_id="sell-reservation-a", processed_at=NOW)
        return order, event, result

    def advance_preopen(self, *, session=NEXT_SESSION, now=NEXT_NOW):
        snapshot, event, unused_created = self.preopen.publish(
            trading_session=session, decision_cutoff=now, created_at=now,
            security_master_version="security-v1",
            corporate_action_version="actions-v1",
            security_status_version="status-v1",
            limit_reference_version="limits-v1", receivables_cutoff=now,
            pending_order_snapshot_id="orders-v1")
        return self.coordinator.accept_preopen_inputs(
            "account-a", event["event_id"], self.account_version(),
            session, 0, snapshot["snapshot_id"], now)

    def phase_event(self, sequence, event_type, *, session=NEXT_SESSION,
                    previous_event_id=None):
        return self.append_event(
            event_type, "account-phase:account-a:{}".format(session),
            {"phase_command": event_type}, sequence=sequence,
            session=session, previous_event_id=previous_event_id)

    def apply_due(self, *, session=NEXT_SESSION, phase_version=1,
                  sequence=1, previous_event_id=None, now=NEXT_NOW):
        event = self.phase_event(
            sequence, "ApplyReceivablesAndCorporateActions",
            session=session, previous_event_id=previous_event_id)
        result = self.broker.apply_due_receivables(
            "account-a", event["event_id"], self.account_version(),
            session, phase_version, now)
        return event, result

    def process_sells(self, quote, *, broker=None, session=NEXT_SESSION,
                      phase_version=2, sequence=2, previous_event_id=None,
                      now=NEXT_NOW):
        broker = broker or self.broker
        event = self.phase_event(
            sequence, "ProcessOpenSells", session=session,
            previous_event_id=previous_event_id)
        result = broker.process_open_sells(
            "account-a", event["event_id"], self.account_version(), session,
            phase_version, {quote.symbol: quote}, now)
        return event, result

    def test_t_plus_one_rejects_same_day_sell_reservation(self):
        order = self.sell_order(valid_session=SESSION)
        event = self.append_event(
            "ApprovedOrderCommitted", "approved-sell:account-a:20261009",
            {"approved_order": asdict(order)}, session=SESSION)
        with self.assertRaisesRegex(ValueError, "T\+1 available"):
            self.broker.register_sell_order(
                "account-a", event["event_id"], self.account_version(), order,
                reservation_id="same-day", processed_at=NOW)
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM orders").fetchone()[0])

    def test_open_sell_closes_trade_and_commits_notification_atomically(self):
        self.register_sell()
        self.advance_preopen()
        receivable_event, unused_result = self.apply_due()
        unused_event, result = self.process_sells(
            DailyOpenQuote("sh600000", 12.0, True, 10.0),
            previous_event_id=receivable_event["event_id"])
        self.assertEqual("filled", result.value["domain_value"]["outcomes"][0]["status"])
        self.assertEqual(1, len(result.value["domain_value"]["fills"]))
        self.assertIsNone(self.row(
            "positions", "account_id=? AND symbol=?",
            ("account-a", "sh600000")))
        self.assertEqual("CLOSED", self.row(
            "logical_trades", "account_id=? AND trade_id=?",
            ("account-a", TRADE_ID))["status"])
        self.assertEqual("CONSUMED", self.row(
            "sell_reservations", "account_id=? AND reservation_id=?",
            ("account-a", "sell-reservation-a"))["status"])
        self.assertEqual(1, self.store.connection.execute(
            "SELECT count(*) FROM fills WHERE side='sell'").fetchone()[0])
        self.assertEqual(1, self.store.connection.execute(
            "SELECT count(*) FROM notification_outbox").fetchone()[0])
        self.assertEqual(2, self.store.connection.execute(
            "SELECT count(*) FROM notification_parts").fetchone()[0])

    def test_limit_down_defers_order_and_preserves_share_reservation(self):
        self.register_sell()
        self.advance_preopen()
        receivable_event, unused_result = self.apply_due()
        unused_event, result = self.process_sells(
            DailyOpenQuote("sh600000", 9.0, True, 9.0),
            previous_event_id=receivable_event["event_id"])
        outcome = result.value["domain_value"]["outcomes"][0]
        self.assertEqual("deferred", outcome["status"])
        self.assertEqual("OPEN_AT_LIMIT_DOWN", outcome["reason_code"])
        self.assertEqual("WAITING", self.row(
            "orders", "account_id=? AND order_id=?",
            ("account-a", "sell-a"))["status"])
        self.assertEqual("ACTIVE", self.row(
            "sell_reservations", "account_id=? AND reservation_id=?",
            ("account-a", "sell-reservation-a"))["status"])
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM notification_outbox").fetchone()[0])

    def test_cash_and_share_receivables_apply_once(self):
        cash = AccountReceivableInstruction(
            "cash-a", "sh600000", "CASH", NEXT_SESSION,
            "cash dividend", cash_micros=20_000_000)
        share = AccountReceivableInstruction(
            "share-a", "sh600000", "SHARE", NEXT_SESSION,
            "bonus shares", quantity=10, trade_id=TRADE_ID)
        previous_event_id = None
        for sequence, instruction in enumerate((cash, share), 1):
            event = self.append_event(
                "CorporateActionReceivableDeclared",
                "corporate-action:account-a:20261009", {
                    "receivable": asdict(instruction)}, sequence=sequence,
                previous_event_id=previous_event_id)
            self.broker.register_receivable(
                "account-a", event["event_id"], self.account_version(),
                instruction, NOW)
            previous_event_id = event["event_id"]
        self.advance_preopen()
        event, result = self.apply_due()
        replay = self.broker.apply_due_receivables(
            "account-a", event["event_id"], result.account_version - 1,
            NEXT_SESSION, 1, NEXT_NOW)
        self.assertTrue(replay.replayed)
        self.assertEqual(20_000_000,
                         result.value["domain_value"]["cash_credited_micros"])
        position = self.row(
            "positions", "account_id=? AND symbol=?",
            ("account-a", "sh600000"))
        self.assertEqual(110, position["quantity"])
        self.assertEqual(110, position["sellable_quantity"])
        self.assertEqual(2_020_000_000, self.row(
            "account_balances", "account_id=?", ("account-a",)
        )["cash_micros"])
        self.assertEqual(2, self.store.connection.execute(
            "SELECT count(*) FROM account_receivables WHERE status='APPLIED'"
        ).fetchone()[0])

    def test_fault_after_sell_facts_rolls_back_fill_cash_lot_and_phase(self):
        self.register_sell()
        self.advance_preopen()
        receivable_event, unused_result = self.apply_due()
        failing = TransactionalDailyBroker(
            self.accounts, self.coordinator, slippage_bps=0,
            ledger=FailAfterSellLedger())
        quote = DailyOpenQuote("sh600000", 12.0, True, 10.0)
        with self.assertRaisesRegex(RuntimeError, "fault after sell facts"):
            self.process_sells(
                quote, broker=failing,
                previous_event_id=receivable_event["event_id"])
        self.assertEqual(0, self.store.connection.execute(
            "SELECT count(*) FROM fills WHERE side='sell'").fetchone()[0])
        self.assertEqual(100, self.row(
            "positions", "account_id=? AND symbol=?",
            ("account-a", "sh600000"))["quantity"])
        self.assertEqual("OPEN", self.row(
            "logical_trades", "account_id=? AND trade_id=?",
            ("account-a", TRADE_ID))["status"])
        self.assertEqual("RECEIVABLES_APPLIED", self.row(
            "account_sessions", "account_id=? AND trading_session=?",
            ("account-a", NEXT_SESSION))["phase"])


if __name__ == "__main__":
    unittest.main()
