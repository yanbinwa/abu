"""Two-stage quality model tests."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuSelectionModel import (
    VCPQualityModel, VCPQualityModelConfig, load_vcp_quality_model_config,
    walk_forward_quality_predictions,
)
from abupy.AlphaBu.ABuWalkForward import WalkForwardConfig


def model_frame(rows=500):
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2022-01-03", periods=250).strftime(
        "%Y%m%d").astype(int).to_numpy()
    x = rng.normal(size=rows)
    noise = rng.normal(scale=.3, size=rows)
    return pd.DataFrame({
        "signal_asof": np.repeat(dates, 2)[:rows],
        "x": x,
        "missing": np.where(np.arange(rows) % 7, x*.5, np.nan),
        "false_breakout_5d": (x+noise < 0).astype(float),
        "excess_return_20d": .02*x+rng.normal(scale=.005, size=rows),
    }), dates


class SelectionModelTest(unittest.TestCase):

    def test_two_stage_score_rewards_return_and_penalizes_failure(self):
        frame, _ = model_frame()
        config = VCPQualityModelConfig(
            minimum_train_rows=100, minimum_class_rows=10)
        model = VCPQualityModel(config, ("x", "missing")).fit(frame)
        test = pd.DataFrame({"x": [-2., 2.], "missing": [np.nan, 1.]})
        prediction = model.predict(test)
        self.assertGreater(prediction.quality_score.iloc[1],
                           prediction.quality_score.iloc[0])
        self.assertGreater(prediction.false_breakout_probability.iloc[0],
                           prediction.false_breakout_probability.iloc[1])

    def test_walk_forward_predictions_are_unique_and_past_trained(self):
        frame, calendar = model_frame()
        config = VCPQualityModelConfig(
            minimum_train_rows=80, minimum_class_rows=5)
        split = WalkForwardConfig(
            minimum_train_dates=50, validation_dates=20, test_dates=20,
            label_horizon_sessions=5)
        result = walk_forward_quality_predictions(
            frame, calendar, config, split, ("x", "missing"))
        self.assertFalse(result.empty)
        self.assertFalse(result.row_index.duplicated().any())
        self.assertTrue((result.train_end < result.validation_start).all())
        self.assertTrue((result.validation_end < result.test_start).all())

    def test_custom_return_target_is_frozen_in_predictions(self):
        frame, _ = model_frame()
        frame["event_path_r_60d"] = (
            frame["excess_return_20d"] * 10)
        config = VCPQualityModelConfig(
            minimum_train_rows=100, minimum_class_rows=10,
            false_breakout_penalty_return=.5)
        model = VCPQualityModel(
            config, ("x", "missing"),
            return_target="event_path_r_60d").fit(frame)
        prediction = model.predict(frame.iloc[:3])
        self.assertTrue(
            (prediction.prediction_target == "event_path_r_60d").all())
        self.assertTrue(np.isfinite(prediction.predicted_target).all())

    def test_config_loader_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"unexpected": 1}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_vcp_quality_model_config(path)


if __name__ == "__main__":
    unittest.main()
