"""Fixed-order lifecycle, price limits and independent-ledger tests."""

import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuPortfolioExecutor import (
    ExecutionConfig, PortfolioExecutor, load_execution_config,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from abupy.AlphaBu.ABuSelectionStrategies import SelectionPanel
from abupy.AlphaBu.ABuTradeIntent import TradeIntent


def make_executor(opens=None, st_known=True, slippage=0):
    dates = np.array([20250102, 20250103, 20250106, 20250107], dtype=np.int32)
    close = np.array([[10.0], [10.0], [9.0], [9.2]], dtype=np.float32)
    opening = np.array(opens or [[10.0], [10.0], [9.0], [9.2]], dtype=np.float32)
    high = np.maximum(opening, close) + 0.1
    low = np.minimum(opening, close) - 0.1
    volume = np.full_like(close, 100_000)
    base = SelectionPanel(
        dates, ("sz000001",), opening, high, low, close, volume,
        np.array([100, 101, 100, 102], dtype=float),
        execution={"open": opening, "high": high, "low": low,
                   "close": close, "volume": volume},
    )
    master = pd.DataFrame({
        "symbol": ["sz000001"], "list_date": ["2000-01-01"],
        "delist_date": [None], "status": ["listed"],
    })
    known = np.full(close.shape, st_known, dtype=bool)
    panel = SelectionPanelV2(base, master, st_status_known=known)
    return PortfolioExecutor(
        panel, ExecutionConfig(initial_cash=100_000, slippage_bps=slippage)
    )


def intent(side="buy", suffix="1", stop=8.0):
    return TradeIntent(
        intent_id="intent-" + suffix, strategy_id="test",
        strategy_version="1", signal_asof=20250102,
        symbol="sz000001", side=side,
        initial_stop_raw=stop if side == "buy" else None,
    )


class PortfolioExecutorTest(unittest.TestCase):

    def test_frozen_execution_config_loads_strictly(self):
        path = Path(__file__).parents[1] / "configs/selection/execution_v2.json"
        config = load_execution_config(path)
        self.assertEqual(config.mode, "pit_corrected")
        self.assertEqual(config.slippage_bps, 25.0)

    def test_quantity_is_fixed_before_open_and_cash_reconciles(self):
        executor = make_executor()
        order, reservation = executor.approve_order(
            intent(), 1000, 20250103, max_buy_price_raw=10.5,
            planned_risk_per_share=2.5,
        )
        self.assertEqual(order.quantity, 1000)
        self.assertGreater(reservation.reserved_cash, 10_500)
        fills = executor.process_open(1)
        self.assertEqual(fills[0].status, "filled")
        self.assertEqual(fills[0].quantity, 1000)
        row = executor.process_close(1)
        self.assertAlmostEqual(row["capital"], row["cash"] + row["stocks"])
        self.assertLess(row["liquidation_nav_3_limits"], row["capital"])
        self.assertLessEqual(row["liquidation_nav_5_limits"],
                             row["liquidation_nav_3_limits"])
        self.assertEqual(executor.reserved_cash, 0)

    def test_different_future_open_does_not_change_approved_quantity(self):
        first = make_executor()
        second = make_executor(opens=[[10.0], [10.4], [9.0], [9.2]])
        order1, _ = first.approve_order(intent(suffix="a"), 900, 20250103, 10.5, 2.5)
        order2, _ = second.approve_order(intent(suffix="a"), 900, 20250103, 10.5, 2.5)
        self.assertEqual(order1.quantity, order2.quantity)
        self.assertEqual(order1.planned_initial_r_cash,
                         order2.planned_initial_r_cash)

    def test_limit_up_buy_is_rejected(self):
        executor = make_executor(opens=[[10.0], [11.0], [9.0], [9.2]])
        order, _ = executor.approve_order(
            intent(), 100, 20250103, max_buy_price_raw=11.0,
            planned_risk_per_share=3.0,
        )
        fill = executor.process_open(1)[0]
        self.assertEqual(fill.status, "rejected")
        self.assertEqual(fill.reason_code, "OPEN_AT_LIMIT_UP")
        self.assertNotIn(order.symbol, executor.positions)

    def test_limit_down_sell_is_deferred_then_filled(self):
        executor = make_executor()
        executor.approve_order(intent(), 100, 20250103, 10.5, 2.5)
        executor.process_open(1)
        executor.process_close(1)
        sell = TradeIntent(
            intent_id="sell", strategy_id="test", strategy_version="1",
            signal_asof=20250103, symbol="sz000001", side="sell",
        )
        executor.approve_order(sell, 100, 20250106)
        first = executor.process_open(2)[0]
        self.assertEqual(first.status, "deferred")
        self.assertEqual(first.reason_code, "OPEN_AT_LIMIT_DOWN")
        second = executor.process_open(3)[0]
        self.assertEqual(second.status, "filled")
        self.assertNotIn("sz000001", executor.positions)

    def test_reservation_rejects_unaffordable_order(self):
        executor = make_executor()
        order, reservation = executor.approve_order(
            intent(), 10_000, 20250103, 20.0, 2.0
        )
        self.assertIsNone(order)
        self.assertEqual(reservation.decision, "rejected")
        self.assertEqual(reservation.reason_codes,
                         ("INSUFFICIENT_CASH_RESERVATION",))

    def test_buy_expires_and_releases_cash(self):
        executor = make_executor()
        executor.approve_order(intent(), 100, 20250103, 10.5, 2.0)
        self.assertGreater(executor.reserved_cash, 0)
        fill = executor.process_open(2)[0]
        self.assertEqual(fill.status, "expired")
        self.assertEqual(executor.reserved_cash, 0)

    def test_corporate_action_receivable_is_credited(self):
        executor = make_executor()
        executor.approve_order(intent(), 100, 20250103, 10.5, 2.0)
        executor.process_open(1)
        executor.panel.base.corporate_actions = {
            1: [{"symbol": 0, "cash_per_share": 0.1,
                 "stock_per_share": 0.1, "cash_day": 2,
                 "stock_day": 2, "description": "test"}]
        }
        executor.process_close(1)
        cash_before = executor.cash
        initial_r_cash = executor.positions["sz000001"].initial_r_cash_frozen
        stop_before = executor.positions["sz000001"].initial_stop_raw
        executor.process_open(2)
        self.assertAlmostEqual(executor.cash - cash_before, 10.0)
        self.assertEqual(executor.positions["sz000001"].quantity, 110)
        self.assertAlmostEqual(executor.positions["sz000001"].initial_stop_raw,
                               stop_before / 1.1)
        self.assertEqual(executor.positions["sz000001"].initial_r_cash_frozen,
                         initial_r_cash)

    def test_corporate_action_odd_lot_can_be_sold_in_full(self):
        executor = make_executor()
        executor.approve_order(intent(), 100, 20250103, 10.5, 2.0)
        executor.process_open(1)
        position = executor.positions["sz000001"]
        executor.positions["sz000001"] = type(position)(
            **{**position.__dict__, "quantity": 110}
        )
        sell = TradeIntent(
            intent_id="sell-odd-lot", strategy_id="test", strategy_version="1",
            signal_asof=20250103, symbol="sz000001", side="sell",
        )
        order, _ = executor.approve_order(sell, 110, 20250106)
        self.assertEqual(order.quantity, 110)

    def test_terminated_position_is_marked_to_zero(self):
        executor = make_executor()
        executor.panel.terminated_mask[2:, 0] = True
        executor.approve_order(intent(), 100, 20250103, 10.5, 2.0)
        executor.process_open(1)
        executor.process_close(1)
        row = executor.process_close(2)
        self.assertEqual(row["stocks"], 0.0)
        self.assertIn("sz000001", executor.positions)


if __name__ == "__main__":
    unittest.main()
