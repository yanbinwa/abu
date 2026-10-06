import unittest

import numpy as np
import pandas as pd

from scripts.validate_alpha158_baostock_fundamental_overlay_v1 import (
    lag_date_map, provider_code, value_composite,
)


class BaoStockFundamentalOverlayTest(unittest.TestCase):
    def test_provider_code(self):
        self.assertEqual(provider_code("sh600000"), "sh.600000")
        self.assertEqual(provider_code("SZ000001"), "sz.000001")
        with self.assertRaises(ValueError):
            provider_code("600000")

    def test_value_composite_rewards_lower_multiples(self):
        frame = pd.DataFrame({
            "signal_asof": [20250102, 20250102],
            "symbol": ["sh600000", "sz000001"],
            "peTTM": [10.0, 20.0],
            "pbMRQ": [1.0, 2.0],
            "psTTM": [2.0, 4.0],
            "pcfNcfTTM": [5.0, 10.0],
        })
        result = value_composite(
            frame, ["peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"], 2, .5)
        scores = result.set_index("symbol").fundamental_percentile
        self.assertGreater(scores["sh600000"], scores["sz000001"])
        self.assertTrue(result.fundamental_data_eligible.all())

    def test_value_composite_keeps_sparse_rows_neutral(self):
        frame = pd.DataFrame({
            "signal_asof": [20250102, 20250102],
            "symbol": ["sh600000", "sz000001"],
            "peTTM": [np.nan, 20.0],
            "pbMRQ": [np.nan, 2.0],
            "psTTM": [4.0, 4.0],
            "pcfNcfTTM": [np.nan, 10.0],
        })
        result = value_composite(
            frame, ["peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"], 2, .5)
        sparse = result[result.symbol.eq("sh600000")].iloc[0]
        self.assertFalse(sparse.fundamental_data_eligible)
        self.assertEqual(sparse.fundamental_percentile, .5)

    def test_lag_date_map_uses_exact_session_offset(self):
        dates = np.array([20241230, 20241231, 20250102, 20250103, 20250106])
        mapping = lag_date_map([20250106], dates, 2)
        self.assertEqual(mapping[20250106], 20250102)


if __name__ == "__main__":
    unittest.main()
