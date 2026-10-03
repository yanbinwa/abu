"""Standardized residual score tests."""
import unittest

import pandas as pd

from scripts.backtest_vcp_standardized_residual_v1 import (
    FEATURES, standardized_residual_scores,
)


class VCPStandardizedResidualTest(unittest.TestCase):

    def test_equal_weight_score_ranks_every_component_within_day(self):
        rows = []
        for symbol, value in (("a", 1.), ("b", 2.), ("c", 3.)):
            row = {"intent_id": symbol, "signal_asof": 20260102,
                   "symbol": symbol}
            row.update({feature: value for feature in FEATURES})
            rows.append(row)
        weights = {feature+"_weight": .25 for feature in FEATURES}
        result = standardized_residual_scores(
            pd.DataFrame(rows), weights).set_index("symbol")
        self.assertEqual(result.loc["a", "standardized_residual_score"], 0.)
        self.assertEqual(result.loc["b", "standardized_residual_score"], .5)
        self.assertEqual(result.loc["c", "standardized_residual_score"], 1.)


if __name__ == "__main__":
    unittest.main()
