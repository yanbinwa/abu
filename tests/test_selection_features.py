"""Point-in-time selection feature tests."""
import unittest

import numpy as np

from abupy.AlphaBu.ABuSelectionFeatures import (
    VCP_QUALITY_FEATURES, VCP_QUALITY_FEATURE_VERSION_V2,
    VCPFeatureSnapshotBuilder,
)
from abupy.AlphaBu.ABuVCPStrategy import VCPStrategy
from tests.test_vcp_strategy import make_vcp_panel


class SelectionFeatureTest(unittest.TestCase):

    def test_feature_snapshot_is_complete_and_future_invariant(self):
        panel, day = make_vcp_panel()
        intents = VCPStrategy(panel).generate_intents(day, "core")
        first = VCPFeatureSnapshotBuilder(panel).build(intents)
        self.assertEqual(len(first), 2)
        self.assertEqual(set(VCP_QUALITY_FEATURES).difference(first.columns), set())
        self.assertTrue((first.signal_asof == int(panel.dates[day])).all())
        panel.close[day+1:] *= 3
        panel.high[day+1:] *= 4
        panel.amount[day+1:] *= 7
        second = VCPFeatureSnapshotBuilder(panel).build(intents)
        np.testing.assert_allclose(
            first[list(VCP_QUALITY_FEATURES)].to_numpy(float),
            second[list(VCP_QUALITY_FEATURES)].to_numpy(float),
            equal_nan=True,
        )
        self.assertEqual(first.feature_config_sha256.tolist(),
                         second.feature_config_sha256.tolist())

    def test_unknown_industry_degrades_to_missing_features(self):
        panel, day = make_vcp_panel()
        panel.industry[day, 0] = -1
        intent = next(item for item in VCPStrategy(panel).generate_intents(
            day, "core") if item.symbol == panel.symbols[0])
        frame = VCPFeatureSnapshotBuilder(panel).build([intent])
        self.assertTrue(np.isnan(frame.iloc[0].industry_return_20d))

    def test_v2_reconstructs_components_missing_from_frozen_metadata(self):
        panel, day = make_vcp_panel()
        intents = VCPStrategy(panel).generate_intents(day, "core")
        frame = VCPFeatureSnapshotBuilder(
            panel, VCP_QUALITY_FEATURE_VERSION_V2).build(intents)
        self.assertTrue(frame.ma120_slope.notna().all())
        self.assertTrue(frame.contraction_tightness.notna().all())
        self.assertTrue(frame.breakout_strength.notna().all())


if __name__ == "__main__":
    unittest.main()
