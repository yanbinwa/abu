import unittest

from scripts.analyze_alpha158_ridge_policy_label_ablation_v1 import gate_audit


class Alpha158RidgePolicyLabelAnalysisTest(unittest.TestCase):

    def test_gate_audit_reports_exposure_and_bootstrap_failures(self):
        costs = {}
        for cost in ("25bp", "40bp", "60bp"):
            costs[cost] = {
                "annualized_uplift_pp": .6,
                "calmar_candidate": .2, "calmar_baseline": .1,
                "cumulative_return_candidate": .2,
                "cumulative_return_baseline": .1,
                "max_drawdown_candidate": -.08,
                "max_drawdown_baseline": -.1,
                "es95_candidate": -.004, "es95_baseline": -.005,
                "three_limit_down_return_candidate": .1,
                "three_limit_down_return_baseline": .05,
                "average_exposure_candidate": .08,
                "average_exposure_baseline": .10,
            }
        evidence = {
            "cost_scenarios": costs,
            "bootstrap_20": {"annualized_return": {"lower": -.001}},
            "positive_year_count": 8,
            "top5_positive_profit_share": .2,
        }
        rows = {row["gate"]: row for row in gate_audit(evidence)}
        self.assertFalse(rows[
            "bootstrap20_annualized_lower_positive"]["passed"])
        self.assertFalse(rows["25bp_average_exposure_gap"]["passed"])
        self.assertTrue(rows["25bp_cumulative_return"]["passed"])


if __name__ == "__main__":
    unittest.main()
