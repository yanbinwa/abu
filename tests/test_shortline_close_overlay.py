"""Close-event meta-model statistical helper tests."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from scripts.validate_shortline_close_event_overlay_v1 import (
    benjamini_hochberg, generate_meta_predictions, validate_config,
)


class ShortLineCloseOverlayTest(unittest.TestCase):

    def test_benjamini_hochberg_is_monotone_and_bounded(self):
        adjusted = benjamini_hochberg({"a": .01, "b": .04, "c": .20})
        self.assertAlmostEqual(adjusted["a"], .03)
        self.assertAlmostEqual(adjusted["b"], .06)
        self.assertAlmostEqual(adjusted["c"], .20)
        self.assertTrue(all(0 <= value <= 1 for value in adjusted.values()))

    def test_config_rejects_strict_pit_claim(self):
        config = {
            "research_status": "RETROSPECTIVE_SCREEN_ONLY_NOT_ADMITTED",
            "strict_pit": True, "availability_evidence": "BACKFILLED_QUERY",
            "automatic_admission": False, "label_horizon_sessions": 20,
            "model": "median_imputer_standard_scaler_ridge",
            "factor_arms": {
                "meta_base": ["base_rank_centered"],
                "meta_stock": ["base_rank_centered"],
                "meta_market": ["base_rank_centered"],
                "meta_combined": ["base_rank_centered"],
            },
            "excluded_from_primary": ["industry"],
        }
        with self.assertRaisesRegex(ValueError, "strict PIT"):
            validate_config(config)

    def test_meta_predictions_are_strictly_purged_and_non_overlapping(self):
        dates = np.arange(20200101, 20200101 + 130)
        rows = []
        for date_index, date in enumerate(dates):
            for symbol_index in range(30):
                rank = symbol_index / 29
                rows.append({
                    "signal_asof": int(date),
                    "symbol": "sz{:06d}".format(symbol_index + 1),
                    "column": symbol_index,
                    "ridge_score": rank,
                    "excess_return_20d": rank - .5,
                    "target_rank": rank - .5,
                    "base_rank_centered": rank - .5,
                    "event_limit_up": float(symbol_index == 29),
                    "event_broken": 0.0, "event_limit_down": 0.0,
                    "event_board_level": float(symbol_index == 29),
                    "event_broken_count": 0.0,
                    "event_log_seal_amount": 0.0,
                    "base_x_sentiment_z63": (rank - .5) * (date_index % 3),
                    "base_x_risk_z63": (rank - .5) * (date_index % 2),
                })
        config = {
            "label_horizon_sessions": 20,
            "walk_forward": {
                "minimum_train_dates": 50, "validation_dates": 20,
                "test_dates": 20, "training_date_stride": 5,
                "embargo_sessions": 0, "ridge_alpha": 100.0,
            },
            "factor_arms": {
                "meta_base": ["base_rank_centered"],
                "meta_stock": ["base_rank_centered", "event_limit_up"],
                "meta_market": ["base_rank_centered",
                                "base_x_sentiment_z63"],
                "meta_combined": ["base_rank_centered", "event_limit_up",
                                  "base_x_sentiment_z63"],
            },
        }
        predictions, folds = generate_meta_predictions(
            pd.DataFrame(rows), dates, config)
        self.assertFalse(predictions.duplicated(
            ["signal_asof", "symbol"]).any())
        self.assertGreater(predictions.signal_asof.nunique(), 0)
        self.assertTrue(all(item["strict_label_non_overlap"] for item in folds))
        self.assertTrue(all(item["train_label_end"] < item["validation_start"]
                            for item in folds))
        self.assertTrue(all(item["validation_label_end"] < item["test_start"]
                            for item in folds))


if __name__ == "__main__":
    unittest.main()
