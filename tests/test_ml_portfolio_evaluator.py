"""M5 paired account bootstrap and concentration tests."""
import unittest

import numpy as np
import pandas as pd

from abupy.MLBu.ABuMLContracts import MLContractError
from abupy.MLBu.ABuMLPortfolioEvaluator import (
    account_metrics, align_account_returns, metric_difference,
    paired_account_bootstrap, top_positive_profit_share,
)


class PortfolioEvaluatorTest(unittest.TestCase):

    def test_non_linear_metrics_are_computed_before_difference(self):
        candidate = np.array([.20, -.20, -.10])
        baseline = np.array([.10, -.10, -.20])
        correct = metric_difference(account_metrics(candidate),
                                    account_metrics(baseline))
        wrong = account_metrics(candidate-baseline)
        self.assertNotAlmostEqual(correct.cumulative_return,
                                  wrong.cumulative_return)
        self.assertNotAlmostEqual(correct.max_drawdown, wrong.max_drawdown)

    def test_identical_accounts_zero_and_swap_reverses_sign(self):
        left = np.array([.01, -.02, .03, -.01]*20)
        same = paired_account_bootstrap(left, left, paths=30)
        for interval in same["intervals"].values():
            self.assertEqual(interval["lower"], 0.0)
            self.assertEqual(interval["upper"], 0.0)
        right = np.array([.005, -.01, .02, -.005]*20)
        forward = paired_account_bootstrap(left, right, paths=30)
        reverse = paired_account_bootstrap(right, left, paths=30)
        for name in forward["intervals"]:
            self.assertAlmostEqual(forward["intervals"][name]["median"],
                                   -reverse["intervals"][name]["median"], 12)

    def test_alignment_rejects_missing_days(self):
        candidate = pd.DataFrame({"date": [1, 2], "net_return": [0, .1]})
        baseline = pd.DataFrame({"date": [1], "net_return": [0]})
        with self.assertRaises(MLContractError):
            align_account_returns(candidate, baseline)

    def test_profit_share_boundaries(self):
        self.assertIsNone(top_positive_profit_share([-1, 0]))
        self.assertEqual(top_positive_profit_share([1]*10), .5)
        self.assertGreater(top_positive_profit_share([6, 1, 1, 1, 1, 1]), .5)


if __name__ == "__main__":
    unittest.main()
