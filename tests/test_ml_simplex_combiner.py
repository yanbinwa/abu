"""M8 seven-family adapter and constrained combiner tests."""
import unittest

import numpy as np
import pandas as pd

from abupy.MLBu.ABuMLContracts import MLContractError
from abupy.MLBu.ABuMLFamilyOOSAudit import FAMILIES
from abupy.MLBu.adapters.ABuAllMeanRankMLAdapter import (
    AllMeanRankMLAdapter, FAMILY_RANK_COLUMNS,
)
from abupy.MLBu.models.ABuSimplexCombiner import (
    EqualWeightCombiner, SimplexCombiner, fit_simplex_weights,
)


def frame(seed=7, dates=(20240101, 20240102, 20240103), rows=80):
    rng = np.random.default_rng(seed)
    values = rng.normal(size=(len(dates)*rows, len(FAMILIES)))
    result = pd.DataFrame(values, columns=FAMILY_RANK_COLUMNS)
    result.insert(0, "symbol", ["sz{:06d}".format(i % rows)
                                 for i in range(len(result))])
    result.insert(0, "signal_asof", np.repeat(dates, rows))
    result["target_rank"] = (.6*values[:, 0]+.2*values[:, 1] +
                             rng.normal(scale=.1, size=len(result)))
    for family in FAMILIES:
        result["family_{}_source_manifest_id".format(family)] = \
            np.repeat(["{}-{}".format(family, date) for date in dates], rows)
        result["family_{}_fold".format(family)] = np.repeat(
            np.arange(len(dates)), rows)
    # An attractive raw factor must remain inaccessible to the adapter.
    result["return_20d"] = result.target_rank
    return result


class SimplexCombinerTest(unittest.TestCase):

    def test_equal_weight_a0_and_feature_isolation(self):
        data = frame()
        adapter = AllMeanRankMLAdapter()
        expected = data[list(FAMILY_RANK_COLUMNS)].mean(axis=1).to_numpy()
        np.testing.assert_allclose(EqualWeightCombiner().predict(data), expected)
        self.assertNotIn("return_20d", adapter.feature_columns)

    def test_simplex_weights_are_nonnegative_and_sum_to_one(self):
        train, valid = frame(), frame(seed=8, dates=(20240201, 20240202))
        model = SimplexCombiner().fit(train, valid)
        self.assertGreaterEqual(model.weights.min(), 0.0)
        self.assertAlmostEqual(model.weights.sum(), 1.0, 12)
        self.assertIn(model.selection["lambda"], (.01, .1, 1., 10.))

    def test_extreme_shrinkage_returns_to_equal_weight(self):
        data = frame()
        weights, _ = fit_simplex_weights(
            data, AllMeanRankMLAdapter(), shrinkage=1e12)
        np.testing.assert_allclose(
            weights, np.repeat(1/7, 7), rtol=0, atol=1e-8)

    def test_future_test_values_cannot_change_fitted_weights(self):
        train, valid = frame(), frame(seed=8, dates=(20240201, 20240202))
        model = SimplexCombiner().fit(train, valid)
        before = model.weights.copy()
        future = frame(seed=9, dates=(20250101,))
        future["target_rank"] *= -1000
        model.predict(future)
        np.testing.assert_allclose(model.weights, before, rtol=0, atol=0)

    def test_missing_source_manifest_fails_closed(self):
        data = frame()
        data.loc[0, "family_regression_trend_source_manifest_id"] = ""
        with self.assertRaises(MLContractError):
            SimplexCombiner().fit(data, frame(seed=8))


if __name__ == "__main__":
    unittest.main()
