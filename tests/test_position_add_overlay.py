# -*- encoding: utf-8 -*-
import unittest
from types import SimpleNamespace

import pandas as pd

from abupy.AlphaBu.ABuPositionAddPolicy import AddProposal
from abupy.AlphaBu.ABuPositionAddResearch import (
    FixedPathOverlayBook, replay_isolated_add_sleeve,
    replay_residual_add_overlay,
)
from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig
from abupy.AlphaBu.ABuPortfolioRisk import PortfolioRiskEngine
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

    def _residual_fixture(self, base_orders=()):
        panel = make_executor(slippage=0).panel
        panel.amount[:] = 10_000_000.0
        proposal = AddProposal(
            "p-residual", "e", "t", "GLOBAL", "sz000001", 20250102, 1,
            "TURTLE_ATR_ADVANCE_MARKET_UP", 500.0, 2000.0, 8.0, 11.0,
            20250103, 70, logical_order_id="o-residual")
        curve = pd.DataFrame({
            "date": panel.dates,
            "cash": [90_000.0]*4, "stocks": [10_000.0]*4,
            "capital": [100_000.0]*4, "exposure": [0.1]*4,
        })
        positions = [{
            "date": 20250102, "symbol": "sz000001", "trade_id": "base",
            "quantity": 100, "market_value": 1000.0, "industry": "UNKNOWN",
            "beta": 1.0, "open_risk": 200.0, "initial_r_cash": 200.0,
            "has_stop": True, "limit_fraction": 0.1, "pending": False,
            "is_add": False, "latest_fill_date": 20250102,
        }]
        return panel, proposal, curve, positions, base_orders

    def test_residual_overlay_freezes_quantity_and_preserves_base_curve(self):
        panel, proposal, curve, positions, orders = self._residual_fixture()
        replay = replay_residual_add_overlay(
            panel, [proposal],
            [SimpleNamespace(trade_id="t", fill_date=20250107)],
            curve, orders, positions,
            ExecutionConfig(initial_cash=100_000.0, slippage_bps=0),
            PortfolioRiskEngine(panel), start_date=20250102,
            end_date=20250107)
        self.assertEqual(replay["approvals"].iloc[0].quantity, 100)
        self.assertEqual(replay["entries"][0].quantity, 100)
        self.assertTrue((replay["curve"].base_capital == 100_000.0).all())
        self.assertEqual(len(replay["dispositions"]), 1)

    def test_residual_overlay_reserves_base_order_cash_first(self):
        order = SimpleNamespace(
            side="buy", position_effect="OPEN", created_asof=20250102,
            max_buy_price_raw=10.0, quantity=8900,
            planned_initial_r_cash=1000.0)
        panel, proposal, curve, positions, _ = self._residual_fixture([order])
        replay = replay_residual_add_overlay(
            panel, [proposal],
            [SimpleNamespace(trade_id="t", fill_date=20250107)],
            curve, [order], positions,
            ExecutionConfig(initial_cash=100_000.0, slippage_bps=0),
            PortfolioRiskEngine(panel), start_date=20250102,
            end_date=20250107)
        self.assertTrue(replay["approvals"].empty)
        self.assertEqual(replay["rejections"].iloc[0].reason,
                         "NO_RESIDUAL_CASH")


if __name__ == "__main__":
    unittest.main()
