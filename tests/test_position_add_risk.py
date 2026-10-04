# -*- encoding: utf-8 -*-
import unittest

from abupy.AlphaBu.ABuPortfolioRisk import PortfolioRiskEngine, RiskConfig
from abupy.AlphaBu.ABuTradeIntent import TradeIntent
from tests.test_portfolio_executor import make_executor


class PositionAddRiskTest(unittest.TestCase):

    def _open(self, executor, trade_id="t1", quantity=100):
        executor.panel.amount[:] = 10_000_000.0
        intent = TradeIntent(
            "open", "s", "1", 20250102, "sz000001",
            signal_price_raw=10.0, initial_stop_raw=8.0,
            trade_id=trade_id, position_effect="OPEN")
        executor.approve_order(intent, quantity, 20250103, 10.5, 2.0)
        executor.process_open(1)

    def _add(self, risk_budget=1000.0, notional=20000.0):
        return TradeIntent(
            "add", "s", "1", 20250103, "sz000001", side="buy",
            signal_price_raw=9.0, initial_stop_raw=8.0,
            trade_id="t1", position_effect="INCREASE",
            metadata={"max_buy_price_raw": 9.3,
                      "risk_budget_cash_cap": risk_budget,
                      "notional_cash_cap": notional})

    def test_existing_symbol_position_reduces_add_headroom(self):
        executor = make_executor()
        self._open(executor, quantity=100)
        engine = PortfolioRiskEngine(executor.panel)
        decision = engine.evaluate(executor, self._add(), 1, 2,
                                   requested_quantity=500)
        absolute_cap = int((decision.equity * engine.config.max_symbol_weight /
                            9.3) // 100 * 100)
        self.assertLess(decision.quantity_symbol_headroom, absolute_cap)

    def test_notional_and_trade_risk_caps_are_applied_and_reserved_once(self):
        executor = make_executor()
        self._open(executor)
        engine = PortfolioRiskEngine(executor.panel)
        add = self._add(risk_budget=300.0, notional=1000.0)
        order, _, decision = engine.approve(
            executor, add, 1, 2, requested_quantity=1000)
        self.assertEqual(decision.quantity_notional_cap, 100)
        self.assertLessEqual(decision.final_quantity, 100)
        trade = executor.position_ledger.logical_trades["t1"]
        reserved = trade.reserved_add_risk_cash
        self.assertGreater(reserved, 0.0)
        duplicate, rejection = executor.approve_order(
            add, 100, 20250106, 9.3, decision.planned_risk_per_share)
        self.assertIsNone(duplicate)
        self.assertEqual(rejection.reason_codes, ("DUPLICATE_ADD_REQUEST",))
        self.assertEqual(executor.position_ledger.logical_trades["t1"].reserved_add_risk_cash,
                         reserved)

    def test_rejection_releases_add_risk_reservation(self):
        executor = make_executor(opens=[[10.0], [10.0], [11.0], [9.2]])
        self._open(executor)
        engine = PortfolioRiskEngine(executor.panel)
        add = self._add()
        order, _, _ = engine.approve(executor, add, 1, 2, requested_quantity=100)
        self.assertIsNotNone(order)
        self.assertGreater(executor.position_ledger.logical_trades["t1"].reserved_add_risk_cash,
                           0.0)
        fill = executor.process_open(2)[0]
        self.assertEqual(fill.status, "rejected")
        self.assertEqual(executor.position_ledger.logical_trades["t1"].reserved_add_risk_cash,
                         0.0)

    def test_post_fill_derisk_intent_is_trade_scoped(self):
        executor = make_executor()
        self._open(executor, quantity=100)
        engine = PortfolioRiskEngine(
            executor.panel, RiskConfig(max_gross_exposure=0.0001))
        review = engine.post_fill_review(executor, 1)
        self.assertTrue(review.de_risk_intents)
        self.assertEqual(review.de_risk_intents[0].trade_id, "t1")
        self.assertEqual(review.de_risk_intents[0].position_effect, "CLOSE")


if __name__ == "__main__":
    unittest.main()
