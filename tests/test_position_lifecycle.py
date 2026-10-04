# -*- encoding: utf-8 -*-
import unittest

from abupy.AlphaBu.ABuPositionLedger import PositionLedger
from abupy.AlphaBu.ABuTradeIntent import TradeIntent
from tests.test_portfolio_executor import make_executor


class PositionLifecycleTest(unittest.TestCase):

    def _open(self, executor, trade_id="t1", quantity=200):
        intent = TradeIntent(
            "open-" + trade_id, "s", "1", 20250102, "sz000001",
            initial_stop_raw=8.0, trade_id=trade_id, position_effect="OPEN")
        executor.approve_order(intent, quantity, 20250103, 10.5, 2.0)
        executor.process_open(1)

    def test_complete_state_transition_table_rejects_illegal_event(self):
        executor = make_executor()
        self._open(executor)
        ledger = executor.position_ledger
        self.assertEqual(ledger.logical_trades["t1"].status, "ACTIVE")
        ledger.request_exit("t1")
        self.assertEqual(ledger.logical_trades["t1"].status, "EXIT_REQUESTED")
        with self.assertRaisesRegex(ValueError, "INVALID_TRADE_STATE_TRANSITION"):
            ledger.transition("t1", "OPEN_FILLED")

    def test_same_day_lot_cannot_be_reserved_for_sale(self):
        executor = make_executor()
        self._open(executor)
        sell = TradeIntent(
            "sell", "s", "1", 20250103, "sz000001", side="sell",
            trade_id="t1", position_effect="CLOSE")
        order, rejection = executor.approve_order(sell, 200, 20250103)
        self.assertIsNone(order)
        self.assertEqual(rejection.reason_codes,
                         ("INVALID_TRADE_LIFECYCLE_OR_T1",))
        sell_next = TradeIntent(
            "sell-next", "s", "1", 20250103, "sz000001", side="sell",
            trade_id="t1", position_effect="CLOSE")
        order, _ = executor.approve_order(sell_next, 200, 20250106)
        self.assertIsNotNone(order)

    def test_exit_cancels_pending_add_and_blocks_fill(self):
        executor = make_executor()
        self._open(executor)
        add = TradeIntent(
            "add", "s", "1", 20250103, "sz000001", side="buy",
            initial_stop_raw=8.0, trade_id="t1", position_effect="INCREASE")
        order, _ = executor.approve_order(add, 100, 20250106, 10.5, 2.0)
        self.assertIsNotNone(order)
        executor.position_ledger.request_exit("t1")
        fill = executor.process_open(2)[0]
        self.assertEqual(fill.status, "rejected")
        self.assertEqual(fill.reason_code, "TARGET_TRADE_NOT_ACTIVE")

    def test_stock_dividend_preserves_book_cost_and_frozen_risk(self):
        executor = make_executor()
        self._open(executor, quantity=100)
        before = executor.position_ledger.lots_for_trade("t1")[0]
        executor.panel.base.corporate_actions = {
            1: [{"symbol": 0, "cash_per_share": 0.0,
                 "stock_per_share": 0.1, "cash_day": None,
                 "stock_day": 2, "description": "split"}]
        }
        executor.process_close(1)
        executor.process_open(2)
        after = executor.position_ledger.lots_for_trade("t1")[0]
        self.assertEqual(after.quantity_remaining, 110)
        self.assertAlmostEqual(after.remaining_book_cost_cash,
                               before.remaining_book_cost_cash)
        self.assertEqual(after.risk_cash_frozen, before.risk_cash_frozen)
        self.assertAlmostEqual(after.stop_raw_at_fill,
                               before.stop_raw_at_fill / 1.1)


if __name__ == "__main__":
    unittest.main()
