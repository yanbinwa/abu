"""Context result statistics tests."""

import unittest

import numpy as np
import pandas as pd

from scripts.analyze_vcp_context_v1 import (
    benjamini_hochberg, cluster_bootstrap_mean_r,
)


class VCPContextAnalysisTest(unittest.TestCase):

    def test_cluster_bootstrap_is_deterministic_and_clusters_same_day(self):
        trades = pd.DataFrame({
            "entry_date": [1, 1, 2, 3], "r": [1., 1., -1., .5]})
        first = cluster_bootstrap_mean_r(trades, simulations=200, seed=7)
        second = cluster_bootstrap_mean_r(trades, simulations=200, seed=7)
        self.assertEqual(first, second)
        self.assertEqual(first["clusters"], 3)
        self.assertTrue(np.isfinite(first["ci_low"]))

    def test_bh_q_values_are_monotone_in_p_order(self):
        q = benjamini_hochberg([.01, .04, .03])
        self.assertTrue((q >= np.array([.01, .04, .03])).all())
        order = np.argsort([.01, .04, .03])
        self.assertTrue(np.all(np.diff(q[order]) >= 0))


if __name__ == "__main__":
    unittest.main()

