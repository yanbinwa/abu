import unittest

from scripts.audit_full_market_float_share_conflicts_v1 import (
    _select_sample, arbitration_label, board, difference_band,
)


class FullMarketFloatShareConflictTest(unittest.TestCase):

    def test_difference_bands_and_boards(self):
        self.assertEqual(difference_band(.006), "00_0.5_to_1pct")
        self.assertEqual(difference_band(.02), "01_1_to_5pct")
        self.assertEqual(difference_band(.10), "02_5_to_20pct")
        self.assertEqual(difference_band(.30), "03_20_to_50pct")
        self.assertEqual(difference_band(.70), "04_above_50pct")
        self.assertEqual(board("sh688001"), "star")
        self.assertEqual(board("sz300001"), "chinext")
        self.assertEqual(board("sz000001"), "main")

    def test_baostock_arbitration(self):
        self.assertEqual(arbitration_label(100, 100, 80, .005),
                         "supports_local_daily")
        self.assertEqual(arbitration_label(80, 100, 80, .005),
                         "supports_cninfo")
        self.assertEqual(arbitration_label(None, 100, 80, .005),
                         "missing_auxiliary")

    def test_sample_keeps_global_top_and_strata(self):
        rows = [{
            "symbol": "sh600001", "conflict_days": 100,
            "board": "main", "median_difference_band": "a",
            "majority_direction": "up",
        }, {
            "symbol": "sz300001", "conflict_days": 50,
            "board": "chinext", "median_difference_band": "b",
            "majority_direction": "down",
        }, {
            "symbol": "sh688001", "conflict_days": 10,
            "board": "star", "median_difference_band": "c",
            "majority_direction": "up",
        }]
        selected = _select_sample(rows, global_top=1, per_stratum=1)
        self.assertEqual(selected, ["sh600001", "sh688001", "sz300001"])


if __name__ == "__main__":
    unittest.main()
