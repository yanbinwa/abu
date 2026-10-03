"""Entry-quality diagnostic tests."""
import unittest

import pandas as pd

from scripts.analyze_vcp_entry_quality import (
    cluster_bootstrap_mean, exit_attribution, selection_uplift,
)


class VCPEntryQualityTest(unittest.TestCase):

    def test_cluster_bootstrap_is_deterministic(self):
        first = cluster_bootstrap_mean([1, 2, 3], paths=500, seed=7)
        second = cluster_bootstrap_mean([1, 2, 3], paths=500, seed=7)
        self.assertEqual(first, second)
        self.assertEqual(first[0], 2.0)

    def test_selection_uplift_compares_against_same_day_candidates(self):
        rows = []
        for date, values in ((20260101, (1.0, 3.0)), (20260102, (2.0, 4.0))):
            for index, value in enumerate(values):
                rows.append({
                    "signal_asof": date, "intent_id": "{}-{}".format(date, index),
                    "return_5d": value, "return_20d": value,
                    "hit_plus_1r_20d": bool(index),
                    "false_breakout_5d": not bool(index),
                })
        frame = pd.DataFrame(rows)
        selected = ["20260101-1", "20260102-1"]
        result = selection_uplift(frame, selected).set_index("metric")
        self.assertEqual(result.loc["return_20d", "cluster_mean_uplift"], 1.0)
        self.assertEqual(
            result.loc["false_breakout_5d", "cluster_mean_uplift"], -0.5)

    def test_exit_reason_is_joined_through_sell_order_and_fill(self):
        orders = pd.DataFrame([{
            "order_id": "sell-1", "symbol": "sz000001", "side": "sell",
            "created_asof": 20260105, "reason": None,
        }])
        fills = pd.DataFrame([{
            "order_id": "sell-1", "date": 20260106, "status": "filled",
        }])
        reasons = pd.DataFrame([{
            "date": 20260105, "symbol": "sz000001", "reason": "INITIAL_STOP",
        }])
        trades = pd.DataFrame([{
            "symbol": "sz000001", "exit_date": 20260106,
            "r": -1.2, "pnl": -100.0,
        }])
        result = exit_attribution(orders, fills, reasons, trades)
        self.assertEqual(result.iloc[0].reason, "INITIAL_STOP")
        self.assertEqual(result.iloc[0].trade_count, 1)
        self.assertEqual(result.iloc[0].pnl, -100.0)


if __name__ == "__main__":
    unittest.main()
