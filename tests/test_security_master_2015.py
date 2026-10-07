"""Tests for the BaoStock 2015 full-market security master."""
from __future__ import annotations

import unittest

import pandas as pd

from scripts.freeze_baostock_security_master_2015_v1 import build_master


class SecurityMaster2015Test(unittest.TestCase):

    def test_keeps_both_exchanges_and_in_scope_delistings(self):
        frame = pd.DataFrame({
            "code": ["sh.600001", "sz.000001", "sh.600002", "sz.000002",
                     "sh.900001"],
            "code_name": ["a", "b", "c", "d", "e"],
            "ipoDate": ["2000-01-01"] * 5,
            "outDate": ["", "", "2016-01-01", "2014-01-01", ""],
            "type": ["1"] * 5, "status": ["1", "1", "0", "0", "1"],
        })
        result = build_master(frame)
        self.assertEqual(result.symbol.tolist(), [
            "sh600001", "sh600002", "sz000001"])
        self.assertEqual(result.status.tolist(), [
            "listed", "delisted", "listed"])

    def test_cutoff_can_extend_the_historical_universe(self):
        frame = pd.DataFrame({
            "code": ["sh.600002", "sz.000002"],
            "code_name": ["a", "b"],
            "ipoDate": ["2000-01-01", "2000-01-01"],
            "outDate": ["2012-06-01", "2014-12-31"],
            "type": ["1", "1"], "status": ["0", "0"],
        })
        result = build_master(frame, cutoff="2012-01-01")
        self.assertEqual(result.symbol.tolist(), ["sh600002", "sz000002"])


if __name__ == "__main__":
    unittest.main()
