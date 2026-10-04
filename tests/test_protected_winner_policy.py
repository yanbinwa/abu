# -*- encoding: utf-8 -*-
import unittest
from dataclasses import replace

from abupy.AlphaBu.ABuPositionAddPolicy import (
    PositionAddContext, ProtectedWinnerPolicy,
    breakeven_price_raw_including_costs,
)
from abupy.AlphaBu.ABuTradeIntent import TradeIntent
from tests.test_portfolio_executor import make_executor


class ProtectedWinnerPolicyTest(unittest.TestCase):

    def _context(self, **overrides):
        executor = make_executor()
        intent = TradeIntent(
            "open", "s", "1", 20250102, "sz000001",
            signal_price_adjusted=10.0, initial_stop_adjusted=9.0,
            initial_stop_raw=9.0, trade_id="t1", position_effect="OPEN")
        executor.approve_order(intent, 100, 20250103, 10.5, 1.5,
                               portfolio_equity_asof=100000.0)
        executor.process_open(1)
        ledger = executor.position_ledger
        trade = replace(
            ledger.logical_trades["t1"], current_stop_raw=10.2,
            entry_session_index=1, last_buy_fill_session_index=1)
        ledger.logical_trades["t1"] = trade
        values = dict(
            signal_asof=20250110, valid_session=20250113,
            trade_snapshot=trade,
            physical_position_snapshot=ledger.physical_positions["sz000001"],
            lot_snapshots=tuple(ledger.lots_for_trade("t1")),
            adjusted_market_window={"close_adjusted": 11.5,
                                    "atr21_adjusted": 1.0,
                                    "last_fill_price_adjusted": 10.0},
            raw_execution_snapshot={"close_raw": 11.5},
            portfolio_risk_snapshot={"portfolio_equity_asof": 100000.0,
                                     "session_index": 6},
            base_signal_status="HOLD", data_version="fixture")
        values.update(overrides)
        return PositionAddContext(**values)

    def test_all_frozen_conditions_trigger_one_proposal(self):
        evaluation = ProtectedWinnerPolicy().evaluate(self._context())
        self.assertTrue(evaluation.triggered)
        self.assertEqual(evaluation.proposal.trigger_code,
                         "STOP_LEVEL_AT_BREAKEVEN")
        self.assertEqual(evaluation.proposal.risk_budget_cash_cap, 125.0)
        self.assertEqual(evaluation.proposal.notional_cash_cap, 2000.0)

    def test_each_material_boundary_fails_closed(self):
        policy = ProtectedWinnerPolicy()
        context = self._context()
        short = replace(context, portfolio_risk_snapshot={
            "portfolio_equity_asof": 100000.0, "session_index": 4})
        self.assertIn("HOLDING_TOO_SHORT", policy.evaluate(short).reason_codes)
        weak = replace(context, adjusted_market_window={
            **context.adjusted_market_window, "close_adjusted": 10.5})
        self.assertIn("PROFIT_BELOW_1R", policy.evaluate(weak).reason_codes)
        no_hold = replace(context, base_signal_status="EXIT")
        self.assertIn("BASE_SIGNAL_NOT_HOLD", policy.evaluate(no_hold).reason_codes)

    def test_missing_field_fails_closed(self):
        context = self._context(adjusted_market_window={"close_adjusted": 11.5})
        evaluation = ProtectedWinnerPolicy().evaluate(context)
        self.assertFalse(evaluation.triggered)
        self.assertIn("atr21_adjusted", evaluation.missing_fields)

    def test_breakeven_includes_minimum_commission_and_slippage(self):
        policy = ProtectedWinnerPolicy()
        price = breakeven_price_raw_including_costs(100, 1005.0, policy.config)
        self.assertGreater(price, 10.05)


if __name__ == "__main__":
    unittest.main()
