"""M2 Ridge plugin parity tests against the frozen Alpha158 model."""
import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuAlpha158Lite import (
    ALPHA158_LITE_FEATURES, Alpha158LiteConfig, Alpha158LiteModel,
)
from abupy.MLBu.models.ABuRidgePlugin import RidgePlugin


def training_frame(seed=7):
    rng = np.random.default_rng(seed)
    dates = np.repeat(np.arange(20250101, 20250107), 200)
    frame = pd.DataFrame(
        rng.normal(size=(len(dates), len(ALPHA158_LITE_FEATURES))),
        columns=ALPHA158_LITE_FEATURES)
    frame.insert(0, "signal_asof", dates)
    frame["target_rank"] = rng.uniform(-0.5, 0.5, len(frame))
    frame.loc[::31, ALPHA158_LITE_FEATURES[3]] = np.nan
    return frame


class RidgePluginParityTest(unittest.TestCase):

    def test_predictions_and_manifest_are_byte_level_equivalent(self):
        frame = training_frame()
        dates = sorted(frame.signal_asof.unique())
        config = Alpha158LiteConfig()
        frozen = Alpha158LiteModel(config).fit(frame, dates)
        plugin = RidgePlugin(config).fit(frame, dates)
        expected = frozen.predict(frame.iloc[:100])
        actual = plugin.predict(frame.iloc[:100])
        self.assertLessEqual(float(np.max(np.abs(actual-expected))), 1e-12)
        self.assertEqual(plugin.manifest, frozen.manifest)

    def test_non_frozen_alpha_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "requires alpha=100"):
            RidgePlugin(Alpha158LiteConfig(ridge_alpha=1.0))


if __name__ == "__main__":
    unittest.main()
