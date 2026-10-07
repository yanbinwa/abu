import unittest

import numpy as np

from abupy.AlphaBu.ABuAlpha158PolicyLabels import (
    Alpha158PolicyLabelBuilder,
)
from tests.test_vcp_strategy import make_vcp_panel


class Alpha158PolicyLabelTest(unittest.TestCase):

    @staticmethod
    def fixture():
        panel, _ = make_vcp_panel()
        day = len(panel.dates)-62
        column = 0
        panel.close[day, column] = 10.0
        panel.exec_close[day, column] = 10.0
        panel.atr21[day:, column] = 1.0
        panel.open[day+1:, column] = 10.0
        panel.exec_open[day+1:, column] = 10.0
        panel.close[day+1:, column] = 10.0
        panel.buy_tradable_mask[day+1:, column] = True
        panel.sell_tradable_mask[day+1:, column] = True
        return panel, day, column

    def test_stop_invalidated_entry_fails_closed(self):
        panel, day, column = self.fixture()
        panel.exec_open[day+1, column] = 7.5
        panel.open[day+1, column] = 7.5
        row = Alpha158PolicyLabelBuilder(panel).build_day(
            day, [column]).iloc[0]
        self.assertFalse(row.entry_executable)
        self.assertEqual(row.entry_reason, "STOP_INVALIDATED")
        self.assertTrue(np.isnan(row.event_path_r_60d))

    def test_initial_stop_uses_next_executable_open_with_slippage(self):
        panel, day, column = self.fixture()
        panel.close[day+1, column] = 7.9
        panel.open[day+2, column] = 7.5
        panel.exec_open[day+2, column] = 7.5
        row = Alpha158PolicyLabelBuilder(panel).build_day(
            day, [column]).iloc[0]
        entry = 10.0*1.0025
        exit_price = 7.5*(1-0.0025)
        expected = (exit_price-entry)/(entry-8.0)
        self.assertTrue(row.entry_executable)
        self.assertEqual(row.event_path_reason_60d, "INITIAL_STOP")
        self.assertAlmostEqual(row.event_path_r_60d, expected)
        self.assertEqual(row.event_path_end_date_60d,
                         int(panel.dates[day+2]))

    def test_label_maturity_is_fixed_not_outcome_dependent(self):
        panel, day, column = self.fixture()
        early = Alpha158PolicyLabelBuilder(panel).build_day(
            day, [column]).iloc[0]
        panel.close[day+1:day+20, column] = 20.0
        later = Alpha158PolicyLabelBuilder(panel).build_day(
            day, [column]).iloc[0]
        self.assertEqual(early.label_fully_mature_date,
                         later.label_fully_mature_date)
        self.assertEqual(early.label_fully_mature_date,
                         int(panel.dates[day+61]))

    def test_stagnation_signal_exits_at_following_open(self):
        panel, day, column = self.fixture()
        row = Alpha158PolicyLabelBuilder(panel).build_day(
            day, [column]).iloc[0]
        self.assertEqual(row.event_path_reason_60d, "STAGNATION")
        self.assertEqual(row.event_path_holding_sessions_60d, 21)
        self.assertEqual(row.event_path_end_date_60d,
                         int(panel.dates[day+21]))

    def test_trailing_stop_has_priority_after_one_r_activation(self):
        panel, day, column = self.fixture()
        entry = 10.0*1.0025
        initial_r = entry-8.0
        panel.close[day+1, column] = entry+initial_r+.5
        panel.close[day+2, column] = 9.4
        panel.open[day+3, column] = 9.8
        panel.exec_open[day+3, column] = 9.8
        row = Alpha158PolicyLabelBuilder(panel).build_day(
            day, [column]).iloc[0]
        self.assertEqual(row.event_path_reason_60d, "TRAILING_STOP")
        self.assertEqual(row.event_path_end_date_60d,
                         int(panel.dates[day+3]))


if __name__ == "__main__":
    unittest.main()
