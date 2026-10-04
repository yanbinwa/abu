# -*- encoding: utf-8 -*-
import unittest
from pathlib import Path

from abupy.AlphaBu.ABuPositionAddPolicy import (
    NoAddPolicy, PositionAddContext, PositionAddPolicyRunner,
    load_no_add_config,
)
from tests.test_portfolio_executor import make_executor
from abupy.AlphaBu.ABuTradeIntent import TradeIntent


class PositionAddPolicyTest(unittest.TestCase):

    def _context(self):
        executor = make_executor()
        intent = TradeIntent(
            "open", "s", "1", 20250102, "sz000001",
            initial_stop_raw=8.0, trade_id="t1", position_effect="OPEN")
        executor.approve_order(intent, 100, 20250103, 10.5, 2.0)
        executor.process_open(1)
        ledger = executor.position_ledger
        return PositionAddContext(
            signal_asof=20250103, valid_session=20250106,
            trade_snapshot=ledger.logical_trades["t1"],
            physical_position_snapshot=ledger.physical_positions["sz000001"],
            lot_snapshots=tuple(ledger.lots_for_trade("t1")),
            data_version="fixture-v1")

    def test_no_add_is_audited_and_never_proposes(self):
        root = Path(__file__).parents[1]
        config = load_no_add_config(root/"configs/selection/no_add_v1.json")
        evaluation = NoAddPolicy(config).evaluate(self._context())
        self.assertFalse(evaluation.triggered)
        self.assertIsNone(evaluation.proposal)
        self.assertEqual(evaluation.reason_codes,
                         ("POLICY_DISABLED_BY_DEFINITION",))

    def test_evaluation_is_idempotent(self):
        runner = PositionAddPolicyRunner(NoAddPolicy())
        context = self._context()
        first = runner.evaluate(context)
        second = runner.evaluate(context)
        self.assertIs(first, second)
        self.assertEqual(len(runner.evaluations), 1)

    def test_no_add_does_not_change_account_state(self):
        executor = make_executor()
        intent = TradeIntent("legacy", "s", "1", 20250102, "sz000001",
                             initial_stop_raw=8.0)
        executor.approve_order(intent, 100, 20250103, 10.5, 2.0)
        executor.process_open(1)
        before = (executor.cash, tuple(executor.orders),
                  executor.positions["sz000001"])
        PositionAddPolicyRunner(NoAddPolicy()).evaluate(self._context())
        after = (executor.cash, tuple(executor.orders),
                 executor.positions["sz000001"])
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
