"""M1 tests for explicit prediction, training, and diagnostic masks."""
import unittest

import numpy as np
import pandas as pd

from abupy.MLBu.ABuMLContracts import FeatureView, LabelContract
from abupy.MLBu.ABuMLDataset import MLResearchDataset


def fixture_frame():
    return pd.DataFrame([
        {
            "strategy_id": "mock", "signal_asof": "2026-01-02T15:00:00+08:00",
            "symbol": "sh600000", "feature_eligible_asof": True,
            "label": 0.8, "label_end": "2026-02-06T15:00:00+08:00",
            "label_available_at": "2026-02-06T15:05:00+08:00",
            "label_mature": True, "label_valid": True, "query_id": "20260102",
            "f1": 1.0, "f2": 2.0,
        },
        {
            "strategy_id": "mock", "signal_asof": "2026-09-30T15:00:00+08:00",
            "symbol": "sz000001", "feature_eligible_asof": True,
            "label": np.nan, "label_end": None, "label_available_at": None,
            "label_mature": False, "label_valid": False, "query_id": "20260930",
            "f1": 3.0, "f2": 4.0,
        },
        {
            "strategy_id": "mock", "signal_asof": "2026-09-30T15:00:00+08:00",
            "symbol": "sz000002", "feature_eligible_asof": False,
            "label": np.nan, "label_end": None, "label_available_at": None,
            "label_mature": False, "label_valid": False, "query_id": "20260930",
            "f1": 9.0, "f2": 9.0,
        },
    ])


class MLResearchDatasetTest(unittest.TestCase):

    def build(self, frame=None):
        return MLResearchDataset(
            fixture_frame() if frame is None else frame,
            FeatureView("features_v1", ("f1", "f2")),
            LabelContract("alpha158_lite_excess20_v1", 20))

    def test_unmature_test_tail_remains_in_prediction_only(self):
        dataset = self.build()
        self.assertEqual(dataset.prediction_rows()["symbol"].tolist(),
                         ["sh600000", "sz000001"])
        self.assertEqual(dataset.training_rows()["symbol"].tolist(),
                         ["sh600000"])
        self.assertEqual(dataset.diagnostic_rows()["symbol"].tolist(),
                         ["sh600000"])

    def test_future_label_changes_do_not_change_prediction_matrix(self):
        original = self.build()
        changed = fixture_frame()
        changed.loc[changed.symbol == "sz000001", "label"] = -999.0
        changed.loc[changed.symbol == "sz000001", "label_end"] = \
            "2026-10-30T15:00:00+08:00"
        other = self.build(changed)
        pd.testing.assert_frame_equal(original.prediction_matrix(),
                                      other.prediction_matrix())

    def test_label_available_at_enforces_asof_cutoff(self):
        dataset = self.build()
        self.assertTrue(dataset.training_rows(
            "2026-02-06T15:04:59+08:00").empty)
        self.assertEqual(len(dataset.training_rows(
            "2026-02-06T15:05:00+08:00")), 1)

    def test_date_equal_weights_equalize_date_totals(self):
        frame = pd.DataFrame({
            "signal_asof": ["a", "a", "b"],
            "symbol": ["x", "y", "z"],
        })
        weights = MLResearchDataset.date_equal_weights(frame)
        self.assertAlmostEqual(weights[:2].sum(), weights[2:].sum(), 12)


if __name__ == "__main__":
    unittest.main()
