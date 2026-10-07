"""M3 frozen ElasticNet model-selection tests."""
import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuAlpha158Lite import ALPHA158_LITE_FEATURES
from abupy.MLBu.models.ABuElasticNetPlugin import (
    ElasticNetFoldFailure, ElasticNetPlugin, select_best_candidate,
)


def frames(seed=11):
    rng = np.random.default_rng(seed)

    def build(dates, rows):
        signal = np.repeat(dates, rows)
        values = rng.normal(size=(len(signal), len(ALPHA158_LITE_FEATURES)))
        frame = pd.DataFrame(values, columns=ALPHA158_LITE_FEATURES)
        frame.insert(0, "signal_asof", signal)
        frame["target_rank"] = (
            0.2*frame[ALPHA158_LITE_FEATURES[0]] -
            0.1*frame[ALPHA158_LITE_FEATURES[4]] +
            rng.normal(scale=0.05, size=len(frame)))
        frame.loc[::47, ALPHA158_LITE_FEATURES[9]] = np.nan
        return frame
    return build([20250101, 20250102, 20250103], 400), \
        build([20250201, 20250202], 200)


class ElasticNetPluginTest(unittest.TestCase):

    def test_grid_is_frozen_and_repeatable(self):
        train, validation = frames()
        first = ElasticNetPlugin().fit(train, validation)
        second = ElasticNetPlugin().fit(train, validation)
        self.assertEqual(len(first.grid_results), 12)
        self.assertIs(first.manifest["precompute"], True)
        self.assertEqual(first.selection, second.selection)
        np.testing.assert_allclose(first.model.coef_, second.model.coef_,
                                   rtol=0, atol=0)
        np.testing.assert_allclose(first.predict(validation),
                                   second.predict(validation), rtol=0, atol=0)

    def test_ties_choose_stronger_regularization(self):
        result = select_best_candidate([
            {"alpha": 0.001, "l1_ratio": 0.5, "mse": 1.0,
             "converged": True},
            {"alpha": 0.01, "l1_ratio": 0.5, "mse": 1.0+5e-13,
             "converged": True},
            {"alpha": 0.01, "l1_ratio": 0.9, "mse": 1.0+8e-13,
             "converged": True},
        ])
        self.assertEqual((result["alpha"], result["l1_ratio"]), (0.01, 0.9))

    def test_all_failed_grid_fails_fold(self):
        with self.assertRaisesRegex(ElasticNetFoldFailure, "all ElasticNet"):
            select_best_candidate([
                {"alpha": 0.1, "l1_ratio": 0.5, "mse": np.nan,
                 "converged": False}])

    def test_test_labels_cannot_affect_validation_selection(self):
        train, validation = frames()
        first = ElasticNetPlugin().fit(train, validation)
        # The plugin never accepts a test frame during fit. Mutating a future
        # test label therefore cannot enter grid selection.
        future = validation.copy()
        future["target_rank"] = future.target_rank * -1000
        before = dict(first.selection)
        first.predict(future)
        self.assertEqual(first.selection, before)

    def test_zero_coefficients_are_reported_without_feature_deletion(self):
        train, validation = frames()
        plugin = ElasticNetPlugin().fit(train, validation)
        self.assertEqual(plugin.manifest["features"],
                         list(ALPHA158_LITE_FEATURES))
        self.assertEqual(len(plugin.manifest["coefficients"]),
                         len(ALPHA158_LITE_FEATURES))


if __name__ == "__main__":
    unittest.main()
