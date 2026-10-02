"""Lifecycle events and conservative valuation tests."""

import unittest

import pandas as pd

from abupy.AlphaBu.ABuSecurityLifecycle import (
    SecurityLifecycleEvent, accounting_mark, build_corporate_action_events,
    build_master_events, build_name_change_events, build_suspension_events,
    events_visible_asof, liquidation_mark,
)


class SecurityLifecycleTest(unittest.TestCase):

    def test_master_events_and_asof_visibility(self):
        master = pd.DataFrame({
            "symbol": ["sz000001"],
            "list_date": ["2020-01-02"],
            "delist_date": ["2026-05-06"],
        })
        events = build_master_events(master)
        self.assertEqual([event.event_type for event in events],
                         ["LISTED", "TERMINATED"])
        self.assertEqual(len(events_visible_asof(events, 20250101)), 1)

    def test_corporate_action_preserves_announcement_and_effective_dates(self):
        frame = pd.DataFrame([{
            "symbol": "sz000001", "实施方案公告日期": "2025-04-01",
            "股权登记日": "2025-05-08", "除权日": "2025-05-09",
            "派息日": "2025-05-09", "股份到账日": "2025-05-09",
            "派息比例": 1.0, "送股比例": 2.0, "转增比例": 1.0,
            "实施方案分红说明": "test",
        }])
        events = build_corporate_action_events(frame)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event.announcement_date == 20250401 for event in events))
        cash = next(event for event in events if event.event_type == "CASH_DIVIDEND")
        stock = next(event for event in events if event.event_type == "STOCK_DIVIDEND")
        self.assertAlmostEqual(cash.cash_per_share, 0.1)
        self.assertAlmostEqual(stock.share_ratio, 0.3)
        self.assertFalse(cash.visible_on(20250331))
        self.assertTrue(cash.visible_on(20250401))

    def test_valuation_is_conservative_after_termination(self):
        self.assertEqual(accounting_mark(float("nan"), 12.0, False), 12.0)
        self.assertEqual(accounting_mark(11.0, 12.0, False), 11.0)
        self.assertEqual(accounting_mark(float("nan"), 12.0, True), 0.0)
        self.assertAlmostEqual(liquidation_mark(10.0, 0.1, 3), 7.29)
        with self.assertRaises(ValueError):
            liquidation_mark(10.0, 1.0, 1)

    def test_event_type_is_validated(self):
        with self.assertRaises(ValueError):
            SecurityLifecycleEvent("x", "UNKNOWN", 20250101)

    def test_name_changes_emit_st_transitions(self):
        frame = pd.DataFrame({
            "证券代码": ["000001", "000001"],
            "变更日期": ["2025-01-02", "2025-02-03"],
            "变更后简称": ["ST测试", "测试股份"],
        })
        events = build_name_change_events(frame)
        self.assertEqual([event.event_type for event in events],
                         ["ST_ENTER", "ST_EXIT"])

    def test_suspension_transitions_are_explicit_events(self):
        dates = [20250102, 20250103, 20250106, 20250107]
        suspended = [[False], [True], [True], [False]]
        universe = [[True], [True], [True], [True]]
        events = build_suspension_events(
            dates, ["sz000001"], suspended, universe
        )
        self.assertEqual([event.event_type for event in events],
                         ["SUSPENDED", "RESUMED"])
        self.assertEqual([event.effective_date for event in events],
                         [20250103, 20250107])


if __name__ == "__main__":
    unittest.main()
