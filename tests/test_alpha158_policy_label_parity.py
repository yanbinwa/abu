import unittest

import pandas as pd

from scripts.validate_alpha158_policy_labels_v1 import summarize_parity


class Alpha158PolicyLabelParityTest(unittest.TestCase):

    def test_censoring_is_separate_from_observed_event_contract(self):
        frame = pd.DataFrame({
            "entry_executable": [True, True],
            "entry_price_match": [True, True],
            "entry_price_abs_error": [0.0, 0.0],
            "label_reason": ["INITIAL_STOP", "TIME_MARK_60"],
            "actual_reason": ["INITIAL_STOP", "TRAILING_STOP"],
            "label_exit_date": [2, 60],
            "actual_exit_date": [2, 90],
            "realized_pnl_cash": [-10.0, 100.0],
            "label_event_r": [-1.0, 1.0],
            "actual_realized_r": [-1.0, 2.0],
            "actual_holding_sessions": [2, 90],
        })
        result = summarize_parity(frame)
        self.assertTrue(result["entry_contract_passed"])
        self.assertTrue(result["observed_event_contract_passed"])
        self.assertEqual(result["observed_event_trades"], 1)
        self.assertEqual(result["censored_or_missing_trades"], 1)
        self.assertEqual(result["censored_realized_pnl_cash"], 100.0)

    def test_observed_reason_mismatch_fails_contract(self):
        frame = pd.DataFrame({
            "entry_executable": [True], "entry_price_match": [True],
            "entry_price_abs_error": [0.0],
            "label_reason": ["INITIAL_STOP"],
            "actual_reason": ["TRAILING_STOP"],
            "label_exit_date": [2], "actual_exit_date": [2],
            "realized_pnl_cash": [1.0],
            "label_event_r": [-1.0], "actual_realized_r": [1.0],
            "actual_holding_sessions": [2],
        })
        self.assertFalse(summarize_parity(frame)[
            "observed_event_contract_passed"])


if __name__ == "__main__":
    unittest.main()
