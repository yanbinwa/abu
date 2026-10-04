# -*- encoding: utf-8 -*-
import unittest
from types import SimpleNamespace

from abupy.AlphaBu.ABuPositionAddPolicy import AddProposal
from abupy.AlphaBu.ABuPositionAddResearch import (
    FixedPathOverlayBook, replay_isolated_add_sleeve,
)
from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig
from tests.test_portfolio_executor import make_executor


class FixedPathOverlayTest(unittest.TestCase):

    def _proposal(self):
        return AddProposal(
            "p", "e", "t", "GLOBAL", "sz000001", 20250103, 1,
            "STOP_LEVEL_AT_BREAKEVEN", 125.0, 2000.0, 10.0, 11.0,
            20250106, 100, logical_order_id="o")

    def test_overlay_has_independent_cashflow_and_fixed_exit(self):
        book = FixedPathOverlayBook(ExecutionConfig(slippage_bps=25.0))
        lot = book.open(self._proposal(), 100, 10.5, 10.0)
        self.assertIsNotNone(lot)
        rows = book.close_trade("t", 20250120, 12.0)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0].realized_pnl_cash, 0.0)
        self.assertFalse(book.lots)

    def test_overlay_rejects_gap_above_frozen_max_price(self):
        book = FixedPathOverlayBook(ExecutionConfig())
        self.assertIsNone(book.open(self._proposal(), 100, 12.0, 10.0))

    def test_isolated_sleeve_is_cash_backed_and_does_not_mutate_base(self):
        base = make_executor(slippage=0)
        panel = base.panel
        proposal = AddProposal(
            "p-sleeve", "e", "t", "GLOBAL", "sz000001", 20250102, 1,
            "TURTLE_ATR_ADVANCE", 1000.0, 5000.0, 8.0, 11.0,
            20250103, 70, logical_order_id="o-sleeve")
        initial_base_cash = base.cash
        replay = replay_isolated_add_sleeve(
            panel, [proposal],
            [SimpleNamespace(trade_id="t", fill_date=20250107)],
            ExecutionConfig(initial_cash=1500.0, slippage_bps=0),
            initial_cash=1500.0, start_date=20250102, end_date=20250107,
            max_gross_exposure=1.0)
        self.assertEqual(len(replay["entries"]), 1)
        self.assertEqual(replay["entries"][0].quantity, 100)
        self.assertEqual(len(replay["dispositions"]), 1)
        self.assertEqual(base.cash, initial_base_cash)
        self.assertAlmostEqual(
            replay["curve"].iloc[-1].capital,
            1500.0+replay["dispositions"][0].realized_pnl_cash)

    def test_isolated_sleeve_rejects_when_cash_below_one_board_lot(self):
        panel = make_executor(slippage=0).panel
        proposal = AddProposal(
            "p-small", "e", "t", "GLOBAL", "sz000001", 20250102, 1,
            "TURTLE_ATR_ADVANCE", 1000.0, 5000.0, 8.0, 11.0,
            20250103, 70, logical_order_id="o-small")
        replay = replay_isolated_add_sleeve(
            panel, [proposal],
            [SimpleNamespace(trade_id="t", fill_date=20250107)],
            ExecutionConfig(initial_cash=900.0, slippage_bps=0),
            initial_cash=900.0, start_date=20250102, end_date=20250107,
            max_gross_exposure=1.0)
        self.assertFalse(replay["entries"])
        self.assertEqual(
            replay["rejections"].iloc[0].reason,
            "SLEEVE_CAPACITY_LT_BOARD_LOT")


if __name__ == "__main__":
    unittest.main()
