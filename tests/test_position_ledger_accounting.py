# -*- encoding: utf-8 -*-
import unittest

from tests.test_portfolio_executor import intent, make_executor
from abupy.AlphaBu.ABuTradeIntent import TradeIntent


class PositionLedgerAccountingTest(unittest.TestCase):

    def _open(self, executor, quantity=200):
        buy = TradeIntent(
            **{**intent().__dict__, "position_effect": "OPEN",
               "trade_id": "trade-1"})
        executor.approve_order(buy, quantity, 20250103, 10.5, 2.0)
        return executor.process_open(1)[0]

    def test_open_and_increase_create_separate_lots(self):
        executor = make_executor()
        self._open(executor, 200)
        add = TradeIntent(
            "add", "test", "1", 20250103, "sz000001", side="buy",
            signal_price_raw=10.0, initial_stop_raw=8.0,
            trade_id="trade-1", position_effect="INCREASE",
            source_policy_id="protected_winner",
        )
        executor.approve_order(add, 100, 20250106, 10.5, 2.0)
        executor.process_open(2)
        lots = executor.position_lots_frame().sort_values("fill_date")
        self.assertEqual(lots["position_effect"].tolist(), ["OPEN", "INCREASE"])
        self.assertEqual(executor.positions["sz000001"].quantity, 300)
        self.assertEqual(executor.position_ledger.logical_trades["trade-1"].add_count, 1)

    def test_fifo_partial_sell_spans_lots_and_conserves_fees(self):
        executor = make_executor(opens=[[10.0], [10.0], [9.0], [9.2]])
        self._open(executor, 200)
        add = TradeIntent(
            "add", "test", "1", 20250103, "sz000001", side="buy",
            initial_stop_raw=8.0, trade_id="trade-1",
            position_effect="INCREASE")
        executor.approve_order(add, 200, 20250106, 9.5, 1.5)
        executor.process_open(2)
        sell = TradeIntent(
            "sell-part", "test", "1", 20250106, "sz000001", side="sell",
            trade_id="trade-1", position_effect="REDUCE")
        executor.approve_order(sell, 300, 20250107)
        fill = executor.process_open(3)[0]
        self.assertEqual(fill.status, "filled")
        dispositions = executor.position_ledger.lot_dispositions
        self.assertEqual([item.disposed_quantity for item in dispositions], [200, 100])
        self.assertAlmostEqual(sum(item.allocated_sell_commission_cash
                                   for item in dispositions), fill.commission)
        self.assertAlmostEqual(sum(item.allocated_stamp_tax_cash
                                   for item in dispositions), fill.stamp_tax)
        self.assertEqual(executor.positions["sz000001"].quantity, 100)
        self.assertEqual(
            executor.position_ledger.logical_trades["trade-1"].status,
            "ACTIVE")
        cash_from_fill = (fill.quantity * fill.fill_price_raw - fill.commission -
                          fill.transfer_fee - fill.stamp_tax)
        self.assertGreater(cash_from_fill, 0)

    def test_two_logical_orders_receive_separate_minimum_commission(self):
        executor = make_executor()
        first = TradeIntent("a", "s1", "1", 20250102, "sz000001",
                            initial_stop_raw=8.0, trade_id="t1",
                            position_effect="OPEN")
        second = TradeIntent("b", "s2", "1", 20250102, "sz000001",
                             initial_stop_raw=8.0, trade_id="t2",
                             position_effect="OPEN")
        executor.approve_order(first, 100, 20250103, 10.5, 2.0)
        executor.approve_order(second, 100, 20250103, 10.5, 2.0)
        fills = executor.process_open(1)
        self.assertEqual([fill.status for fill in fills], ["filled", "filled"])
        self.assertEqual([fill.commission for fill in fills], [5.0, 5.0])
        self.assertEqual(len(executor.fill_allocations_frame()), 2)


if __name__ == "__main__":
    unittest.main()
