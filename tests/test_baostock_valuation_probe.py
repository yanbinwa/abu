"""Deterministic BaoStock valuation probe diagnostics."""
import unittest

from scripts.probe_baostock_valuation_pit_v1 import (
    match_changes, valuation_diagnostics,
)


class BaoStockValuationProbeTest(unittest.TestCase):
    def test_implied_eps_change_and_event_match(self):
        rows = [
            {"date": "2025-04-28", "close": "10", "peTTM": "10",
             "tradestatus": "1"},
            {"date": "2025-04-29", "close": "11", "peTTM": "11",
             "tradestatus": "1"},
            {"date": "2025-04-30", "close": "12", "peTTM": "6",
             "tradestatus": "1"},
        ]
        _, changes = valuation_diagnostics(rows, .005)
        self.assertEqual(len(changes), 1)
        matched = match_changes(changes, ["2025-04-29"], 10)
        self.assertEqual(matched[0]["matched_event"], "2025-04-29")

    def test_blank_pe_does_not_create_false_change(self):
        rows = [
            {"date": "2025-01-01", "close": "10", "peTTM": "",
             "tradestatus": "1"},
            {"date": "2025-01-02", "close": "10", "peTTM": "10",
             "tradestatus": "1"},
        ]
        _, changes = valuation_diagnostics(rows, .005)
        self.assertEqual(changes, [])


if __name__ == "__main__":
    unittest.main()
