# -*- encoding: utf-8 -*-
import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuPositionAddAnalytics import (
    build_trade_attribution, holm_adjust, partition_matchable_actual_ids,
    run_matched_add_placebos,
)
from abupy.AlphaBu.ABuTradeIntent import TradeIntent
from abupy.AlphaBu.ABuTradeVisualization import build_position_add_markers
from tests.test_portfolio_executor import make_executor


class PositionAddAnalyticsTest(unittest.TestCase):

    def test_same_symbol_logical_exit_does_not_consume_other_strategy_lots(self):
        executor = make_executor(opens=[[10.0], [10.0], [9.5], [9.2]])
        for trade_id, strategy in (("a", "alpha"), ("v", "vcp")):
            intent = TradeIntent(
                "open-"+trade_id, strategy, "1", 20250102, "sz000001",
                initial_stop_raw=8.0, trade_id=trade_id,
                position_effect="OPEN")
            executor.approve_order(intent, 100, 20250103, 10.5, 2.0)
        executor.process_open(1)
        sell = TradeIntent(
            "sell-a", "alpha", "1", 20250103, "sz000001", side="sell",
            trade_id="a", position_effect="CLOSE")
        executor.approve_order(sell, 100, 20250106)
        executor.process_open(2)
        self.assertEqual(executor.position_ledger.quantity_for_trade("a"), 0)
        self.assertEqual(executor.position_ledger.quantity_for_trade("v"), 100)
        self.assertEqual(executor.positions["sz000001"].quantity, 100)

    def test_five_level_attribution_conserves_realized_pnl(self):
        executor = make_executor()
        intent = TradeIntent(
            "open", "vcp", "1", 20250102, "sz000001",
            initial_stop_raw=8.0, trade_id="t", position_effect="OPEN")
        executor.approve_order(intent, 100, 20250103, 10.5, 2.0)
        executor.process_open(1)
        sell = TradeIntent(
            "sell", "vcp", "1", 20250103, "sz000001", side="sell",
            trade_id="t", position_effect="CLOSE")
        executor.approve_order(sell, 100, 20250106)
        executor.process_open(2)
        ledger = executor.position_ledger
        facts, summaries = build_trade_attribution(
            ledger.logical_trades.values(), ledger.lots.values(),
            ledger.lot_dispositions)
        for frame in summaries.values():
            self.assertAlmostEqual(frame.realized_pnl_cash.sum(),
                                   facts.realized_pnl_cash.sum())

    def test_placebo_is_reproducible_and_ignores_future_exit_date(self):
        rows = []
        for index in range(6):
            rows.append({
                "candidate_id": "c{}".format(index), "signal_asof": 20250101,
                "industry_asof": "10", "market_state": "UP",
                "holding_age_bucket": "5-10", "floating_r_bucket": "1-2",
                "liquidity_bucket": "HIGH", "outcome_pnl_cash": index*10.0,
                "future_exit_date": 20250120+index,
            })
        frame = pd.DataFrame(rows)
        first = run_matched_add_placebos(frame, ["c0"], paths=100, seed=7)
        changed = frame.copy()
        changed["future_exit_date"] += 10000
        second = run_matched_add_placebos(changed, ["c0"], paths=100, seed=7)
        np.testing.assert_array_equal(first["distribution"], second["distribution"])
        matched, unmatched = partition_matchable_actual_ids(frame, ["c0"])
        self.assertEqual(matched, ["c0"])
        self.assertEqual(unmatched, [])

    def test_holm_adjustment_is_monotonic_in_sorted_order(self):
        adjusted = holm_adjust([0.01, 0.04, 0.03])
        self.assertTrue(np.all((adjusted >= 0) & (adjusted <= 1)))
        self.assertGreaterEqual(adjusted[1], adjusted[0])

    def test_visual_markers_trace_every_fill_allocation(self):
        executor = make_executor(opens=[[10.0], [10.0], [9.5], [9.2]])
        opening = TradeIntent(
            "open", "vcp", "1", 20250102, "sz000001",
            initial_stop_raw=8.0, trade_id="t", position_effect="OPEN")
        executor.approve_order(opening, 100, 20250103, 10.5, 2.0)
        executor.process_open(1)
        add = TradeIntent(
            "add", "vcp", "1", 20250103, "sz000001",
            initial_stop_raw=8.0, trade_id="t", position_effect="INCREASE",
            source_policy_id="turtle")
        executor.approve_order(add, 100, 20250106, 10.0, 1.5)
        executor.process_open(2)
        sell = TradeIntent(
            "sell", "vcp", "1", 20250106, "sz000001", side="sell",
            trade_id="t", position_effect="CLOSE",
            metadata={"exit_reason": "TRAILING_STOP"})
        executor.approve_order(sell, 200, 20250107)
        executor.process_open(3)
        markers = build_position_add_markers(
            executor.fills_frame(), executor.fill_allocations_frame(),
            executor.lot_dispositions_frame())
        self.assertEqual(len(markers), len(executor.fill_allocations_frame()))
        self.assertEqual(set(markers.marker_type), {"OPEN", "INCREASE", "CLOSE"})
        self.assertFalse(markers.reason.str.lower().eq("nan").any())
        self.assertEqual(
            markers.loc[markers.marker_type.eq("INCREASE"),
                        "source_policy_id"].iloc[0], "turtle")
        self.assertEqual(
            markers.loc[markers.marker_type.eq("CLOSE"), "reason"].iloc[0],
            "移动止损")


if __name__ == "__main__":
    unittest.main()
