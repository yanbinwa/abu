import unittest

import pandas as pd

from scripts.analyze_alpha158_policy_label_alignment_v1 import summarize_day


class Alpha158PolicyLabelAlignmentTest(unittest.TestCase):

    def test_daily_summary_keeps_old_and_event_targets_separate(self):
        frame = pd.DataFrame({
            "signal_asof": [20250102]*4,
            "symbol": ["a", "b", "c", "d"],
            "old_target_rank": [-.5, -.2, .2, .5],
            "event_target_rank": [.5, .2, -.2, -.5],
            "event_path_r_60d": [2., 1., -1., -2.],
            "a0_score": [-.5, -.2, .2, .5],
            "a1_score": [.5, .2, -.2, -.5],
        })
        result = summarize_day(frame)
        self.assertAlmostEqual(result["old_event_label_corr"], -1.0)
        self.assertAlmostEqual(result["a0_old_ic"], 1.0)
        self.assertAlmostEqual(result["a0_event_ic"], -1.0)
        self.assertAlmostEqual(result["a1_event_ic"], 1.0)
        self.assertEqual(result["event_target_rows"], 4)

    def test_missing_event_target_is_excluded_only_from_event_metrics(self):
        frame = pd.DataFrame({
            "signal_asof": [20250102]*4,
            "symbol": ["a", "b", "c", "d"],
            "old_target_rank": [-.5, -.2, .2, .5],
            "event_target_rank": [-.5, -.2, .2, float("nan")],
            "event_path_r_60d": [-2., -1., 1., float("nan")],
            "a0_score": [-.5, -.2, .2, .5],
            "a1_score": [-.5, -.2, .2, .5],
        })
        result = summarize_day(frame)
        self.assertEqual(result["event_target_rows"], 3)
        self.assertAlmostEqual(result["a0_old_ic"], 1.0)
        self.assertAlmostEqual(result["a0_event_ic"], 1.0)


if __name__ == "__main__":
    unittest.main()
