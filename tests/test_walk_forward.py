"""Purged walk-forward and trial registry tests."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuTrialRegistry import (
    read_trial_registry, register_trial,
)
from abupy.AlphaBu.ABuWalkForward import (
    PurgedWalkForward, WalkForwardConfig,
)


class WalkForwardTest(unittest.TestCase):

    def test_folds_are_expanding_and_labels_do_not_cross_boundaries(self):
        calendar = pd.bdate_range("2024-01-02", periods=180).strftime(
            "%Y%m%d").astype(int).to_numpy()
        samples = np.repeat(calendar[::2], 3)
        config = WalkForwardConfig(
            minimum_train_dates=20, validation_dates=10, test_dates=10,
            label_horizon_sessions=5, embargo_sessions=2)
        folds = list(PurgedWalkForward(config).split(samples, calendar))
        self.assertGreaterEqual(len(folds), 2)
        positions = {value: index for index, value in enumerate(calendar)}
        for fold in folds:
            self.assertLessEqual(
                positions[fold.train_end]+7, positions[fold.validation_start])
            self.assertLessEqual(
                positions[fold.validation_end]+7, positions[fold.test_start])
            self.assertTrue(set(fold.train_indices).isdisjoint(
                fold.validation_indices))
            self.assertTrue(set(fold.test_indices).isdisjoint(
                fold.validation_indices))
        self.assertLessEqual(len(folds[0].train_indices),
                             len(folds[-1].train_indices))

    def test_trial_registry_is_append_only_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/trials.jsonl"
            first = register_trial(path, "a", "first", {"x": 1})
            second = register_trial(path, "b", "second", {"x": 2})
            self.assertEqual(second["previous_sha256"], first["record_sha256"])
            self.assertEqual(len(read_trial_registry(path)), 2)
            with self.assertRaises(ValueError):
                register_trial(path, "a", "duplicate", {})
            lines = Path(path).read_text(encoding="utf-8").splitlines()
            record = json.loads(lines[0]); record["hypothesis"] = "changed"
            lines[0] = json.dumps(record)
            Path(path).write_text("\n".join(lines)+"\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_trial_registry(path)


if __name__ == "__main__":
    unittest.main()
