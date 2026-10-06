"""VWAP unit and scale audit tests."""
import unittest

import numpy as np

from scripts.audit_alpha158_vwap_scale_v1 import (
    evaluate_unit_candidates, range_membership,
)


class Alpha158VwapScaleAuditTest(unittest.TestCase):

    def test_share_volume_unit_is_uniquely_recognized(self):
        close = np.array([10.0, 20.0, 30.0])
        volume = np.array([100.0, 200.0, 300.0])
        amount = close * volume
        low = close * .98
        high = close * 1.02
        values, rows = evaluate_unit_candidates(
            amount, volume, close, low, high,
            [.1, 1.0, 10.0], 0.0, .99, .98, 1.02)
        self.assertTrue(np.allclose(values, close))
        self.assertEqual(
            [row["multiplier"] for row in rows if row["recognized"]], [1.0])

    def test_adjustment_mapping_preserves_price_range(self):
        raw_vwap = np.array([10.0, 20.0])
        adjustment = np.array([.5, 2.0])
        adjusted = raw_vwap * adjustment
        valid, inside = range_membership(
            adjusted, np.array([4.9, 39.0]), np.array([5.1, 41.0]), 0.0)
        self.assertTrue(valid.all())
        self.assertTrue(inside.all())

    def test_zero_volume_is_not_a_comparable_unit_row(self):
        values, rows = evaluate_unit_candidates(
            np.array([100.0]), np.array([0.0]), np.array([10.0]),
            np.array([9.0]), np.array([11.0]), [1.0],
            0.0, .99, .98, 1.02)
        self.assertTrue(np.isnan(values[0]))
        self.assertEqual(rows[0]["comparable_rows"], 0)
        self.assertFalse(rows[0]["recognized"])


if __name__ == "__main__":
    unittest.main()
