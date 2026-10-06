"""Canonical-style Alpha158 feature family tests."""
import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuAlpha158Canonical import (
    ALPHA158_CANONICAL_FAMILIES, KBAR_FEATURES, VWAP_FEATURES,
    Alpha158CanonicalFeatureEngine,
    _extreme_index, _positive_negative_ratio, _rolling_ols, _time_rank,
)
from abupy.AlphaBu.ABuAlpha158Lite import Alpha158LiteConfig
from tests.test_vcp_strategy import make_vcp_panel


class Alpha158CanonicalTest(unittest.TestCase):

    def test_family_names_are_unique_and_complete(self):
        names = [name for family in ALPHA158_CANONICAL_FAMILIES.values()
                 for name in family]
        self.assertEqual(len(names), 130)
        self.assertEqual(len(names), len(set(names)))

    def test_kbar_shape_matches_registered_formulas(self):
        panel, day = make_vcp_panel()
        column = 0
        panel.open[day, column] = 10.0
        panel.high[day, column] = 13.0
        panel.low[day, column] = 9.0
        panel.close[day, column] = 12.0
        source = Alpha158LiteConfig(
            minimum_history_sessions=120,
            minimum_median_amount_20d=1.0)
        raw = Alpha158CanonicalFeatureEngine(
            panel, source, "kbar_shape").raw_family(day)
        expected = {
            "KMID": .2, "KLEN": .4, "KMID2": .5,
            "KUP": .1, "KUP2": .25,
            "KLOW": .1, "KLOW2": .25,
            "KSFT": .2, "KSFT2": .5,
        }
        self.assertEqual(tuple(raw), KBAR_FEATURES)
        for name, value in expected.items():
            self.assertAlmostEqual(raw[name][column], value)

    def test_vwap_is_mapped_from_raw_to_signal_price_scale(self):
        panel, day = make_vcp_panel()
        column = 0
        panel.exec_close = panel.exec_close.copy()
        panel.exec_volume = panel.exec_volume.copy()
        panel.exec_volume[day, column] = 100.0
        panel.amount[day, column] = 1050.0
        panel.exec_close[day, column] = 10.0
        panel.close[day, column] = 5.0
        source = Alpha158LiteConfig(
            minimum_history_sessions=120,
            minimum_median_amount_20d=1.0)
        raw = Alpha158CanonicalFeatureEngine(
            panel, source, "vwap_price").raw_family(day)
        self.assertEqual(tuple(raw), VWAP_FEATURES)
        self.assertAlmostEqual(raw["VWAP0"][column], 1.05)
        panel.amount[day, column] = np.nan
        missing = Alpha158CanonicalFeatureEngine(
            panel, source, "vwap_price").raw_family(day)
        self.assertTrue(np.isnan(missing["VWAP0"][column]))

    def test_rolling_ols_matches_linear_series(self):
        values = np.column_stack([
            2.0 + 3.0 * np.arange(1, 6),
            np.full(5, 7.0),
        ])
        slope, rsquare, residual = _rolling_ols(values, minimum=4)
        self.assertAlmostEqual(slope[0], 3.0)
        self.assertAlmostEqual(rsquare[0], 1.0)
        self.assertAlmostEqual(residual[0], 0.0)
        self.assertTrue(np.isnan(rsquare[1]))

    def test_rank_and_extreme_index_follow_qlib_position_semantics(self):
        values = np.array([[1.0], [4.0], [2.0], [3.0]])
        self.assertAlmostEqual(_time_rank(values, 3)[0], .75)
        self.assertAlmostEqual(_extreme_index(values, "max", 3)[0], 2.0)
        self.assertAlmostEqual(_extreme_index(values, "min", 3)[0], 1.0)

    def test_positive_negative_ratios_partition_absolute_change(self):
        changes = np.array([[2.0], [-1.0], [3.0], [-4.0]])
        positive, negative, difference = _positive_negative_ratio(changes, 4)
        self.assertAlmostEqual(positive[0], .5)
        self.assertAlmostEqual(negative[0], .5)
        self.assertAlmostEqual(difference[0], 0.0)

    def test_each_family_snapshot_is_future_invariant(self):
        for family, names in ALPHA158_CANONICAL_FAMILIES.items():
            panel, day = make_vcp_panel()
            source = Alpha158LiteConfig(
                minimum_history_sessions=120,
                minimum_median_amount_20d=1.0)
            engine = Alpha158CanonicalFeatureEngine(panel, source, family)
            first = engine.snapshot(day, include_labels=False)
            panel.open[day+1:] *= 7
            panel.high[day+1:] *= 7
            panel.low[day+1:] *= 7
            panel.close[day+1:] *= 7
            panel.volume[day+1:] *= 11
            second = engine.snapshot(day, include_labels=False)
            pd.testing.assert_frame_equal(first, second)
            self.assertTrue(set(names).issubset(first.columns))


if __name__ == "__main__":
    unittest.main()
