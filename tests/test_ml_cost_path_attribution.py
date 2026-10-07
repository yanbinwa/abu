import unittest

import pandas as pd

from scripts.analyze_alpha158_ml_cost_path_v1 import (
    buy_order_overlap, first_divergence,
)


class MLCostPathAttributionTest(unittest.TestCase):

    def test_first_divergence_distinguishes_value_and_key_changes(self):
        left = pd.DataFrame({
            "date": [1, 2], "symbol": ["a", "b"], "quantity": [100, 100]})
        value_changed = pd.DataFrame({
            "date": [1, 2], "symbol": ["a", "b"], "quantity": [100, 200]})
        key_changed = pd.DataFrame({
            "date": [1, 2], "symbol": ["a", "c"], "quantity": [100, 100]})
        self.assertEqual(
            first_divergence(left, value_changed, "date", ["symbol"],
                             ["quantity"]),
            {"date": 2, "difference": "quantity"})
        self.assertEqual(
            first_divergence(left, key_changed, "date", ["symbol"],
                             ["quantity"]),
            {"date": 2, "difference": "keys"})

    def test_buy_order_overlap_is_dated_and_symmetric(self):
        left = pd.DataFrame({
            "created_asof": [1, 1, 2], "symbol": ["a", "b", "c"],
            "side": ["buy", "buy", "buy"]})
        right = pd.DataFrame({
            "created_asof": [1, 2, 2], "symbol": ["a", "c", "d"],
            "side": ["buy", "buy", "buy"]})
        result = buy_order_overlap(left, right)
        reverse = buy_order_overlap(right, left)
        self.assertEqual(result, reverse)
        self.assertEqual(result["evaluated_order_dates"], 2)
        self.assertEqual(result["identical_order_dates"], 0)
        self.assertAlmostEqual(result["pooled_symbol_jaccard"], 2 / 4)


if __name__ == "__main__":
    unittest.main()
