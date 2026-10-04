# -*- encoding: utf-8 -*-
import unittest

from abupy.AlphaBu.ABuPositionAddPolicy import AddProposal
from abupy.AlphaBu.ABuPositionAddResearch import FixedPathOverlayBook
from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig


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


if __name__ == "__main__":
    unittest.main()
