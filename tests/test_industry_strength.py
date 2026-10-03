"""PIT industry strength and trend-leader tests."""

import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuIndustryStrength import (
    IndustryStrengthBuilder, IndustryTrendLeaderBuilder,
)
from abupy.AlphaBu.ABuMarketBreadth import MarketIndustryContextConfig
from tests.test_market_breadth import make_context_panel


def test_config():
    return MarketIndustryContextConfig(
        minimum_industry_members=2,
        minimum_member_coverage=0.5,
        minimum_capture_sessions=5,
    )


class IndustryStrengthTest(unittest.TestCase):

    def test_industry_returns_and_names_are_reproducible(self):
        panel = make_context_panel()
        day = 200; date = int(panel.dates[day])
        result = IndustryStrengthBuilder(panel, test_config()).build(date, date)
        self.assertEqual(set(result.industry_name), {"行业甲"})
        # 行业乙 only has one strict member after ST/unknown exclusions.
        row = result.iloc[0]
        expected = np.mean(panel.close[day, :3] / panel.close[day - 20, :3] - 1)
        self.assertAlmostEqual(row.return_20d, expected, places=7)
        self.assertGreater(row.breadth_above_ma20, 0.99)

    def test_future_industry_change_does_not_change_past(self):
        panel = make_context_panel()
        day = 210; date = int(panel.dates[day])
        first = IndustryStrengthBuilder(panel, test_config()).build(date, date)
        panel.industry[day + 1:, 0] = 1
        second = IndustryStrengthBuilder(panel, test_config()).build(date, date)
        pd.testing.assert_frame_equal(first, second)

    def test_trend_leader_ranks_inside_current_industry(self):
        panel = make_context_panel(periods=320)
        day = 280; date = int(panel.dates[day])
        frame = IndustryTrendLeaderBuilder(panel, test_config()).build(date, date)
        industry = frame[frame.industry_id == 0].set_index("symbol")
        self.assertEqual(len(industry), 3)
        self.assertGreater(
            industry.loc["sz000001", "relative_return_20d_rank_within_industry"],
            industry.loc["sz000003", "relative_return_20d_rank_within_industry"],
        )
        self.assertTrue((industry.coverage_ratio > 0.5).all())

    def test_trend_leader_is_future_invariant(self):
        panel = make_context_panel(periods=320)
        day = 280; date = int(panel.dates[day])
        first = IndustryTrendLeaderBuilder(panel, test_config()).build(date, date)
        panel.close[day + 1:] *= 10
        panel.high[day + 1:] *= 10
        second = IndustryTrendLeaderBuilder(panel, test_config()).build(date, date)
        pd.testing.assert_frame_equal(first, second)


if __name__ == "__main__":
    unittest.main()
