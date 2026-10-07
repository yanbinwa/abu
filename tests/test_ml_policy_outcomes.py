import unittest

import pandas as pd

from scripts.analyze_alpha158_ml_policy_outcomes_v1 import (
    allocate_cash_events, compare_arms, score_diagnostics,
)


class MLPolicyOutcomesTest(unittest.TestCase):

    def test_cash_event_is_allocated_only_inside_trade_lifetime(self):
        trades = pd.DataFrame({
            "trade_id": ["t1", "t2"], "symbol": ["a", "a"],
            "opened_at": [1, 6], "closed_at": [5, 10],
        })
        events = pd.DataFrame({
            "date": [0, 3, 8, 11], "symbol": ["a"]*4,
            "cash_delta": [10.0, 20.0, 30.0, 40.0],
        })
        result = allocate_cash_events(trades, events)
        self.assertEqual(result.to_dict(), {"t1": 20.0, "t2": 30.0})

    def test_invalid_r_is_excluded_and_rank_correlation_is_finite(self):
        frame = pd.DataFrame({
            "trade_id": ["a", "b", "c", "d", "e", "bad"],
            "valid_r": [True]*5+[False],
            "realized_r": [-1.0, -0.5, 0.0, 0.5, 1.0, float("nan")],
            "realized_pnl_cash": [-10, -5, 0, 5, 10, 3],
            "risk_cash_frozen": [10, 10, 10, 10, 10, 0],
            "cash_event_pnl": [0, 0, 1, 0, 0, 0],
            "a0_score": [1, 2, 3, 4, 5, 100],
            "a1_score": [1, 2, 3, 4, 5, -100],
            "score_delta": [0, 0, 0, 0, 0, -200],
            "exit_reason": ["X"]*6,
        })
        result = score_diagnostics(frame)
        self.assertEqual(result["valid_r_trades"], 5)
        self.assertEqual(result["invalid_r_trades"], 1)
        self.assertAlmostEqual(result["score_spearman"]["a0_score"], 1.0)
        self.assertEqual(result["cash_event_counted_trades"], 1)

    def test_cross_arm_comparison_separates_common_and_unique_keys(self):
        columns = {
            "valid_r": [True, True], "risk_cash_frozen": [10.0, 20.0],
            "realized_pnl_cash": [1.0, 4.0], "realized_r": [0.1, 0.2],
        }
        left = pd.DataFrame({
            **columns, "signal_asof": [1, 2], "symbol": ["a", "b"]})
        right = pd.DataFrame({
            **columns, "signal_asof": [1, 3], "symbol": ["a", "c"]})
        result = compare_arms(left, right)
        self.assertEqual(result["common_key_count"], 1)
        self.assertEqual(result["left_only_key_count"], 1)
        self.assertEqual(result["right_only_key_count"], 1)
        self.assertAlmostEqual(result["selection_jaccard"], 1/3)


if __name__ == "__main__":
    unittest.main()
