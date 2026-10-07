import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuAlpha158Lite import ALPHA158_LITE_FEATURES
from scripts.run_alpha158_ridge_policy_label_ablation_v1 import (
    common_training_rows, factor_diagnostics,
)


class Alpha158RidgePolicyLabelAblationTest(unittest.TestCase):

    @staticmethod
    def frame():
        rows = pd.DataFrame({
            "signal_asof": [20250102]*4,
            "symbol": ["a", "b", "c", "d"],
            "target_rank": [-.5, -.2, .2, .5],
            "event_target_rank": [.5, .2, -.2, np.nan],
            "excess20_ridge_score": [-.5, -.2, .2, .5],
            "event_r60_ridge_score": [.5, .2, -.2, -.5],
        })
        for number, feature in enumerate(ALPHA158_LITE_FEATURES):
            rows[feature] = float(number)
        return rows

    def test_common_rows_remove_label_availability_as_a_confounder(self):
        result = common_training_rows(self.frame())
        self.assertEqual(result.symbol.tolist(), ["a", "b", "c"])
        self.assertTrue(result.target_rank.notna().all())
        self.assertTrue(result.event_target_rank.notna().all())

    def test_diagnostics_do_not_filter_predictions_by_future_event_label(self):
        frame = self.frame()
        result = factor_diagnostics(frame)
        self.assertEqual(len(frame), 4)
        self.assertEqual(result["ridge_excess20_common_v1"][
            "target_rank"]["dates"], 1)
        self.assertEqual(result["ridge_event_r60_v1"][
            "event_target_rank"]["dates"], 1)


if __name__ == "__main__":
    unittest.main()
