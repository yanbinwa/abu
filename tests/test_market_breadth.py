"""Price-independent PIT market breadth tests."""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuMarketBreadth import (
    MarketBreadthBuilder, MarketIndustryContextConfig,
    load_market_industry_context_config,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from abupy.AlphaBu.ABuSelectionStrategies import SelectionPanel


def make_context_panel(periods=280):
    dates = pd.bdate_range("2024-01-02", periods=periods).strftime(
        "%Y%m%d").astype(int).to_numpy()
    symbols = tuple("sz{:06d}".format(index + 1) for index in range(6))
    time = np.arange(periods, dtype=float)
    close = np.column_stack([
        10 + time * 0.030,
        10 + time * 0.020,
        10 + time * 0.010,
        20 - time * 0.010,
        20 + time * 0.005,
        15 + time * 0.015,
    ]).astype(np.float32)
    opening = close.copy(); high = close * 1.01; low = close * 0.99
    volume = np.full(close.shape, 1000, dtype=np.float32)
    amount = np.tile(np.array([10, 9, 8, 7, 6, 5], dtype=np.float32),
                     (periods, 1)) * 1_000_000
    industry = np.tile(np.array([0, 0, 0, 1, 1, 1], dtype=np.int16),
                       (periods, 1))
    st = np.zeros(close.shape, dtype=bool)
    st[:, 4] = True
    benchmark = 100 + time * 0.08 + np.sin(time / 7) * 0.2
    base = SelectionPanel(
        dates, symbols, opening, high, low, close, volume, benchmark,
        execution={"open": opening, "high": high, "low": low,
                   "close": close, "volume": volume},
        amount=amount, market_cap=np.full(close.shape, 1e9),
        industry=industry, st_status=st,
        industry_labels={0: "行业甲", 1: "行业乙"},
    )
    master = pd.DataFrame({
        "symbol": symbols, "list_date": ["2000-01-01"] * len(symbols),
        "delist_date": [None] * len(symbols), "status": ["listed"] * len(symbols),
    })
    known = np.ones(close.shape, dtype=bool)
    known[:, 5] = False
    return SelectionPanelV2(base, master, turnover=np.ones(close.shape),
                            st_status_known=known)


class MarketBreadthTest(unittest.TestCase):

    def test_config_is_strict_and_hash_is_stable(self):
        payload = json.loads(Path(
            "configs/selection/market_industry_context_v1.json"
        ).read_text())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(payload))
            first = load_market_industry_context_config(path)
            second = load_market_industry_context_config(path)
            self.assertEqual(first.sha256, second.sha256)
            payload["unexpected"] = 1
            path.write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                load_market_industry_context_config(path)

    def test_full_and_signal_scopes_use_distinct_denominators(self):
        panel = make_context_panel()
        date = int(panel.dates[150])
        result = MarketBreadthBuilder(panel).build(date, date).set_index(
            "universe_scope")
        self.assertEqual(result.loc["full_eligible", "eligible_universe_count"], 6)
        # Known ST and unknown ST are both excluded from the strict scope.
        self.assertEqual(result.loc["signal_eligible", "eligible_universe_count"], 4)
        self.assertEqual(result.loc["full_eligible", "return_denominator"], 6)
        self.assertEqual(result.loc["signal_eligible", "return_denominator"], 4)
        self.assertEqual(result.loc["full_eligible", "known_st_count"], 1)
        self.assertEqual(result.loc["full_eligible", "unknown_st_count"], 1)

    def test_future_prices_do_not_change_past_features(self):
        panel = make_context_panel()
        day = 200; date = int(panel.dates[day])
        first = MarketBreadthBuilder(panel).build(date, date)
        panel.close[day + 1:] *= 5
        panel.high[day + 1:] *= 5
        panel.low[day + 1:] *= 5
        second = MarketBreadthBuilder(panel).build(date, date)
        pd.testing.assert_frame_equal(first, second)

    def test_suspended_security_is_counted_but_not_imputed(self):
        panel = make_context_panel()
        day = 180; date = int(panel.dates[day])
        panel.base.close[day, 0] = np.nan
        panel.signal_price_available[day, 0] = False
        panel.suspended_mask[day, 0] = True
        result = MarketBreadthBuilder(panel).build(date, date).set_index(
            "universe_scope")
        self.assertEqual(result.loc["full_eligible", "suspended_count"], 1)
        self.assertEqual(result.loc["full_eligible", "return_denominator"], 5)
        self.assertEqual(result.loc["full_eligible", "eligible_universe_count"], 6)


if __name__ == "__main__":
    unittest.main()
