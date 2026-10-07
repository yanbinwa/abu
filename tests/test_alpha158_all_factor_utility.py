"""All-factor utility experiment invariants."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.validate_alpha158_all_factor_utility_v1 import (
    ARMS, curve_cagr, load_ensemble, paired_cagr_interval, validate_config,
)


class AllFactorUtilityTest(unittest.TestCase):

    def config(self):
        path = (Path(__file__).resolve().parents[1] /
                "configs/selection/alpha158_all_factor_utility_v1.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_config_freezes_inventory_and_current_exit_policy(self):
        config = self.config()
        validate_config(config)
        config["review_overlay"] = "ranking_exits_enabled"
        with self.assertRaisesRegex(ValueError, "disabled"):
            validate_config(config)

    def test_config_rejects_inventory_drift(self):
        config = self.config()
        config["inventory"]["total_registered_factors"] += 1
        with self.assertRaisesRegex(ValueError, "inventory"):
            validate_config(config)

    def test_ensemble_arms_are_fixed(self):
        self.assertEqual(
            ARMS, ("baseline", "all_mean_rank", "all_median_rank"))

    def test_equal_and_median_aggregation_use_every_family(self):
        frame = pd.DataFrame({
            "signal_asof": [1, 1],
            "a_rank": [-.5, .5], "b_rank": [.5, -.5],
            "c_rank": [.1, .3],
        })
        columns = ["a_rank", "b_rank", "c_rank"]
        frame["mean"] = frame[columns].mean(axis=1)
        frame["median"] = frame[columns].median(axis=1)
        np.testing.assert_allclose(frame["mean"], [.0333333333, .1])
        np.testing.assert_allclose(frame["median"], [.1, .3])

    def test_paired_cagr_interval_uses_cagr_difference(self):
        dates = [20200101, 20200401, 20200701, 20201001, 20210101]
        baseline = pd.DataFrame({
            "date": dates, "capital": [100., 102., 104., 106., 108.]})
        candidate = pd.DataFrame({
            "date": dates, "capital": [100., 103., 106., 109., 112.]})
        config = self.config()
        interval = paired_cagr_interval(
            baseline, candidate, config, seed=20261006)
        expected = curve_cagr(candidate) - curve_cagr(baseline)
        np.testing.assert_allclose(interval, [expected, expected])


if __name__ == "__main__":
    unittest.main()
