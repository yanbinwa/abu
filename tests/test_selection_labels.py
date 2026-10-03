"""Execution-aware label tests."""
import unittest

import numpy as np

from abupy.AlphaBu.ABuSelectionLabels import (
    VCP_LABEL_VERSION_V2, VCPLabelBuilder,
)
from abupy.AlphaBu.ABuVCPStrategy import VCPStrategy
from tests.test_vcp_strategy import make_vcp_panel


class SelectionLabelTest(unittest.TestCase):

    def test_labels_begin_at_next_open_and_use_frozen_breakout(self):
        panel, day = make_vcp_panel()
        intent = VCPStrategy(panel).generate_intents(day, "core")[0]
        column = panel.symbol_index[intent.symbol]
        panel.open[day+1, column] = 10.0
        panel.exec_open[day+1, column] = 10.0
        panel.close[day+5, column] = 11.0
        panel.high[day+1:day+6, column] = 10.4
        panel.low[day+1:day+6, column] = 9.7
        panel.close[day+1:day+6, column] = 9.9
        panel.close[day+5, column] = 11.0
        row = VCPLabelBuilder(panel).build([intent]).iloc[0]
        self.assertTrue(row.entry_executable)
        self.assertAlmostEqual(row.return_5d, .1)
        self.assertTrue(row.false_breakout_5d)

    def test_unexecutable_gap_has_missing_outcomes(self):
        panel, day = make_vcp_panel()
        intent = VCPStrategy(panel).generate_intents(day, "core")[0]
        column = panel.symbol_index[intent.symbol]
        panel.exec_open[day+1, column] = float(
            intent.metadata["max_buy_price_raw"] + 1)
        row = VCPLabelBuilder(panel).build([intent]).iloc[0]
        self.assertFalse(row.entry_executable)
        self.assertTrue(np.isnan(row.return_20d))
        self.assertTrue(np.isnan(row.false_breakout_5d))

    def test_event_path_label_uses_next_open_after_stop_signal(self):
        panel, day = make_vcp_panel()
        intent = VCPStrategy(panel).generate_intents(day, "core")[0]
        column = panel.symbol_index[intent.symbol]
        panel.open[day+1, column] = 10.5
        panel.exec_open[day+1, column] = 10.5
        panel.close[day+1, column] = intent.initial_stop_adjusted-.01
        panel.open[day+2, column] = 9.8
        row = VCPLabelBuilder(
            panel, label_version=VCP_LABEL_VERSION_V2).build([intent]).iloc[0]
        expected = ((9.8-10.5) /
                    (10.5-intent.initial_stop_adjusted))
        self.assertAlmostEqual(row.event_path_r_60d, expected, places=6)
        self.assertEqual(row.event_path_reason_60d, "INITIAL_STOP")
        self.assertEqual(row.event_path_end_date_60d,
                         int(panel.dates[day+2]))


if __name__ == "__main__":
    unittest.main()
