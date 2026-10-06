"""Alpha158 marginal rank-band diagnostics tests."""
import unittest

import pandas as pd

from scripts.analyze_alpha158_marginal_bands_v1 import daily_band_returns


class Alpha158MarginalBandsTest(unittest.TestCase):

    def test_band_membership_is_disjoint_and_cost_is_applied(self):
        rows = []
        for date in (20250102, 20250103):
            for value in range(50):
                rows.append({
                    "signal_asof": date,
                    "symbol": "s{:02d}".format(value),
                    "score": 50 - value,
                    "excess_return_20d": value / 1000.0,
                })
        daily = daily_band_returns(
            pd.DataFrame(rows), "score", ((1, 10), (11, 20)), .0075)
        self.assertEqual(len(daily), 4)
        self.assertTrue((daily.securities == 10).all())
        self.assertTrue((daily.net_excess == daily.gross_excess - .0075).all())
        first = daily[daily.band == "1-10"].gross_excess.mean()
        second = daily[daily.band == "11-20"].gross_excess.mean()
        self.assertLess(first, second)


if __name__ == "__main__":
    unittest.main()
