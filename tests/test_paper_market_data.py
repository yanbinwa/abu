"""Fail-closed normalization tests for the daily paper-market snapshot."""
from __future__ import annotations

import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from scripts.update_paper_market_data import build_append_records, normalize_spot


class PaperMarketDataTest(unittest.TestCase):

    def test_spot_normalization_and_adjusted_append(self):
        source = pd.DataFrame([{
            "代码": "000001", "名称": "平安银行", "最新价": 11.0,
            "今开": 10.5, "最高": 11.2, "最低": 10.4, "昨收": 10.0,
            "成交量": 1000, "成交额": 1_080_000,
            "换手率": 1.5, "流通市值": 110_000_000,
        }])
        spot = normalize_spot(source, 20261009)
        self.assertEqual(spot.iloc[0].symbol, "sz000001")
        self.assertEqual(spot.iloc[0].volume, 100_000)
        self.assertAlmostEqual(spot.iloc[0].turnover, 0.015)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            signal, raw = root / "signal", root / "raw"
            signal.mkdir(); raw.mkdir()
            with (signal / "sz000001_20200101_20261002").open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "date_time", "open", "high", "low", "close", "pre_close",
                    "volume", "p_change", "date", "date_week", "key"])
                writer.writeheader(); writer.writerow({
                    "date_time": "2026-09-30", "open": 9, "high": 9,
                    "low": 9, "close": 9, "pre_close": 8.9, "volume": 1,
                    "p_change": 1, "date": 20260930, "date_week": 2, "key": 100})
            with (raw / "sz000001.csv").open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "date", "open", "high", "low", "close", "volume",
                    "amount", "outstanding_share", "turnover"])
                writer.writeheader(); writer.writerow({
                    "date": 20260930, "open": 10, "high": 10, "low": 10,
                    "close": 10, "volume": 1, "amount": 1,
                    "outstanding_share": 1, "turnover": .01})
            operations, skipped = build_append_records(
                spot, signal, raw, 20261009)
        self.assertEqual(len(operations), 2)
        adjusted = next(row for path, _, row in operations if path.parent.name == "signal")
        self.assertAlmostEqual(adjusted["close"], 9.9)
        self.assertAlmostEqual(adjusted["pre_close"], 9.0)
        self.assertEqual(skipped["missing_files"], 0)


if __name__ == "__main__":
    unittest.main()
