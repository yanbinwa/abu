"""PIT masks and breadth denominator tests for SelectionPanelV2."""

import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from abupy.AlphaBu.ABuSelectionStrategies import SelectionPanel


def make_panel():
    dates = pd.bdate_range("2025-01-02", periods=35).strftime("%Y%m%d").astype(int)
    shape = (len(dates), 2)
    close = np.column_stack((
        np.linspace(10, 12, len(dates)),
        np.linspace(20, 18, len(dates)),
    )).astype(np.float32)
    opening = close.copy()
    high = close * 1.01
    low = close * 0.99
    volume = np.full(shape, 1000, dtype=np.float32)
    # The first symbol is suspended for the final 22 market sessions.
    opening[-22:, 0] = np.nan
    high[-22:, 0] = np.nan
    low[-22:, 0] = np.nan
    close[-22:, 0] = np.nan
    volume[-22:, 0] = 0
    base = SelectionPanel(
        dates, ("sz000001", "sh600000"), opening, high, low, close,
        volume, np.linspace(100, 110, len(dates)),
        execution={"open": opening, "high": high, "low": low,
                   "close": close, "volume": volume},
        amount=np.full(shape, 1_000_000, dtype=np.float32),
        st_status=np.zeros(shape, dtype=bool),
    )
    master = pd.DataFrame({
        "symbol": ["sz000001", "sh600000"],
        "list_date": ["2025-01-10", "2025-01-02"],
        "delist_date": [None, dates[-3:].astype(str)[0]],
        "status": ["listed", "delisted"],
    })
    turnover = np.full(shape, 0.5, dtype=np.float32)
    return SelectionPanelV2(base, master, turnover=turnover,
                            long_suspension_sessions=20)


class SelectionPanelV2Test(unittest.TestCase):

    def test_universe_uses_listing_and_delisting_dates(self):
        panel = make_panel()
        self.assertFalse(panel.universe_mask[0, 0])
        listed_day = int(np.flatnonzero(panel.dates >= 20250110)[0])
        self.assertTrue(panel.universe_mask[listed_day, 0])
        self.assertFalse(panel.universe_mask[-1, 1])
        self.assertIn("AFTER_DELIST_DATE",
                      panel.eligibility_reasons(-1, 1, min_history=1))

    def test_suspension_stays_in_universe_but_leaves_breadth(self):
        panel = make_panel()
        self.assertTrue(panel.universe_mask[-1, 0])
        self.assertTrue(panel.long_suspension_mask[-1, 0])
        self.assertFalse(panel.breadth_denominator(min_history=1)[-1, 0])

    def test_unknown_shanghai_st_is_excluded_from_strict_sample(self):
        panel = make_panel()
        day = 10
        strict = panel.signal_eligible(min_history=1, unknown_st_policy="exclude")
        sensitivity = panel.signal_eligible(min_history=1, unknown_st_policy="include")
        self.assertFalse(strict[day, 1])
        self.assertTrue(sensitivity[day, 1])
        self.assertIn("UNKNOWN_ST_STATUS",
                      panel.eligibility_reasons(day, 1, min_history=1))

    def test_required_attention_fields_and_reasons(self):
        panel = make_panel()
        panel.turnover[10, 0] = np.nan
        eligible = panel.signal_eligible(
            min_history=1, required_fields=("amount", "turnover")
        )
        self.assertFalse(eligible[10, 0])
        self.assertIn("MISSING_TURNOVER", panel.eligibility_reasons(
            10, 0, min_history=1, required_fields=("amount", "turnover")
        ))

    def test_future_delist_change_does_not_change_past_universe(self):
        panel = make_panel()
        past = panel.universe_mask[10].copy()
        panel.delist_date[1] += 10000
        self.assertTrue(np.array_equal(past, panel.universe_mask[10]))


if __name__ == "__main__":
    unittest.main()
