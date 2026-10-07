"""Coverage and normalization invariants for the 2015 history expansion."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.expand_alpha158_history_2015_data_v1 import (
    filter_universe, merge_raw, normalize_raw, validate_config,
)


class Alpha158History2015DataTest(unittest.TestCase):

    def config(self):
        path = (Path(__file__).resolve().parents[1] /
                "configs/selection/alpha158_history_2015_data_v1.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_config_freezes_unadjusted_execution_prices(self):
        config = self.config()
        validate_config(config)
        config["source_adjustflag_raw"] = "2"
        with self.assertRaisesRegex(ValueError, "unadjusted"):
            validate_config(config)

    def test_universe_keeps_active_and_in_scope_delisted_shenzhen(self):
        frame = pd.DataFrame({
            "code": ["sz.000001", "sz.000002", "sz.000003", "sh.600000"],
            "code_name": ["a", "b", "c", "d"],
            "ipoDate": ["1991-01-01"] * 4,
            "outDate": ["", "2018-01-01", "2014-12-31", ""],
            "type": ["1"] * 4, "status": ["1", "0", "0", "1"],
        })
        result = filter_universe(frame, self.config())
        self.assertEqual(result.symbol.tolist(), ["sz000001", "sz000002"])

    def test_raw_normalization_converts_turnover_and_float_shares(self):
        fields = self.config()["raw_fields"]
        rows = [[
            "2015-01-05", "sz.000001", "10", "11", "9", "10.5", "10",
            "1000", "10000", "2", "5", "1", "0"]]
        result = normalize_raw(rows, fields)
        self.assertAlmostEqual(result.iloc[0].turnover, .02)
        self.assertAlmostEqual(result.iloc[0].outstanding_share, 50000.)

    def test_existing_rows_win_at_merge_boundary(self):
        prefix = pd.DataFrame({
            "date": [20191231, 20200102], "close": [1., 2.]})
        existing = pd.DataFrame({
            "date": [20200102], "close": [3.]})
        result = merge_raw(prefix, existing)
        self.assertEqual(result.date.tolist(), [20191231, 20200102])
        self.assertEqual(result.close.tolist(), [1., 3.])
        self.assertTrue(np.isnan(result.iloc[0].amount))


if __name__ == "__main__":
    unittest.main()
