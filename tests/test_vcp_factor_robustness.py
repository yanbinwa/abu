"""Large-sample robustness calculation tests."""
import unittest

import pandas as pd

from scripts.analyze_vcp_factor_robustness_v1 import (
    bh_adjust, experiment_tables, permutation_test, within_date_ic,
)


class VCPFactorRobustnessTest(unittest.TestCase):

    def test_within_date_ic_does_not_mix_market_dates(self):
        frame = pd.DataFrame({
            "signal_asof": [1] * 5 + [2] * 5,
            "factor": [1, 2, 3, 4, 5] * 2,
            "return_20d": [1, 2, 3, 4, 5] + [5, 4, 3, 2, 1],
        })
        values = within_date_ic(frame, "factor")
        self.assertEqual(values.ic.tolist(), [1.0, -1.0])

    def test_permutation_is_deterministic_and_detects_positive_order(self):
        rows = []
        for date in range(10):
            for value in range(8):
                rows.append({
                    "signal_asof": date, "factor": value,
                    "return_20d": value,
                })
        frame = pd.DataFrame(rows)
        first = permutation_test(frame, "factor", paths=500, seed=3)
        second = permutation_test(frame, "factor", paths=500, seed=3)
        self.assertEqual(first, second)
        self.assertEqual(first["observed_mean_ic"], 1.0)
        self.assertLess(first["positive_direction_p"], .01)

    def test_experiment_increment_is_measured_against_frozen_baseline(self):
        results = pd.DataFrame([
            {"experiment": "baseline_replay", "return_pct": 2.0,
             "max_drawdown_pct": -5.0,
             "daily_expected_shortfall_95_pct": -1.0},
            {"experiment": "new", "return_pct": 1.0,
             "max_drawdown_pct": -4.0,
             "daily_expected_shortfall_95_pct": -.8},
        ])
        annual = pd.DataFrame([
            {"experiment": "baseline_replay", "year": 2025,
             "return_pct": 2.0},
            {"experiment": "new", "year": 2025, "return_pct": 1.0},
        ])
        comparison, by_year = experiment_tables(results, annual)
        row = comparison.set_index("experiment").loc["new"]
        self.assertEqual(row.incremental_return_vs_baseline_pct_points, -1.0)
        self.assertEqual(row.drawdown_improvement_vs_baseline_pct_points, 1.0)
        self.assertEqual(by_year.iloc[0].incremental_return_pct_points, -1.0)

    def test_bh_adjustment_is_monotone_in_p_order(self):
        adjusted = bh_adjust([.01, .04, .03])
        self.assertEqual(adjusted.round(3).tolist(), [.03, .04, .04])


if __name__ == "__main__":
    unittest.main()
