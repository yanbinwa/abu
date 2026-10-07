import unittest

import pandas as pd

from scripts.audit_alpha158_policy_label_coverage_v1 import CoverageAccumulator


class Alpha158PolicyLabelCoverageTest(unittest.TestCase):

    def test_coverage_separates_entry_failures_and_time_marks(self):
        frame = pd.DataFrame({
            "signal_asof": [20200102]*4,
            "entry_executable": [True, True, True, False],
            "entry_reason": ["ELIGIBLE", "ELIGIBLE", "ELIGIBLE",
                             "STOP_INVALIDATED"],
            "event_path_r_60d": [-1.0, 2.0, float("nan"), float("nan")],
            "event_path_reason_60d": ["INITIAL_STOP", "TIME_MARK_60",
                                      "EXIT_PENDING_AT_HORIZON", None],
        })
        audit = CoverageAccumulator()
        audit.add(frame)
        result = audit.payload()
        self.assertEqual(result["rows"], 4)
        self.assertEqual(result["entry_executable_rows"], 3)
        self.assertEqual(result["valid_target_rows"], 2)
        self.assertAlmostEqual(result["valid_target_rate_executable"], 2/3)
        self.assertEqual(result["time_mark_60_rows"], 1)
        self.assertEqual(result["entry_reason_counts"]["STOP_INVALIDATED"], 1)


if __name__ == "__main__":
    unittest.main()
