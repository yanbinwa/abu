import unittest

import numpy as np
import pandas as pd

from scripts.analyze_alpha158_ridge_policy_label_fixed_count_v1 import (
    BASELINE, CANDIDATE, summarize_fixed_count_day,
)


class Alpha158RidgePolicyLabelFixedCountTest(unittest.TestCase):

    def test_selection_does_not_backfill_missing_future_label(self):
        frame = pd.DataFrame({
            "signal_asof": [20250102]*12,
            "symbol": ["s{:02d}".format(item) for item in range(12)],
            "event_path_r_60d": [np.nan, *range(1, 12)],
            "excess20_ridge_score": list(range(12, 0, -1)),
            "event_r60_ridge_score": list(range(12, 0, -1)),
        })
        result = summarize_fixed_count_day(frame)
        self.assertEqual(result[BASELINE+"_top10_selected"], 10)
        self.assertEqual(result[BASELINE+"_top10_labeled"], 9)
        self.assertAlmostEqual(result[BASELINE+"_top10_coverage"], .9)
        self.assertEqual(result[BASELINE+"_top10_mean_r"], 5.0)
        self.assertEqual(result[CANDIDATE+"_top10_mean_r"], 5.0)


if __name__ == "__main__":
    unittest.main()
