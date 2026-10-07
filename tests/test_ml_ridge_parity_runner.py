"""M2 logical prediction parity comparator tests."""
import unittest

import pandas as pd

from scripts.validate_alpha158_ml_ridge_parity_v1 import compare_predictions


def frame(scores=(0.2, 0.1)):
    return pd.DataFrame({
        "signal_asof": [20260102, 20260102],
        "symbol": ["sh600000", "sz000001"],
        "column": [0, 1], "alpha_score": list(scores),
        "baseline_score": [0.0, 0.0],
        "excess_return_20d": [0.1, -0.1], "target_rank": [0.25, -0.25],
        "fold": [0, 0], "train_end": [20251201, 20251201],
        "test_start": [20260102, 20260102],
        "test_end": [20260331, 20260331],
    })


class RidgeParityRunnerTest(unittest.TestCase):

    def test_equal_predictions_pass(self):
        result = compare_predictions(frame(), frame().iloc[::-1])
        self.assertTrue(result["keys_exact"])
        self.assertEqual(result["max_abs_score_error"], 0.0)

    def test_score_and_rank_changes_fail(self):
        with self.assertRaisesRegex(AssertionError, "score parity"):
            compare_predictions(frame(), frame((0.2, 0.1000001)))
        with self.assertRaisesRegex(AssertionError, "daily Ridge ranks"):
            compare_predictions(frame((0.2, 0.1)), frame((0.1, 0.2)))


if __name__ == "__main__":
    unittest.main()
