"""Separated B1/B2 legacy strategy adapter tests."""

import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig, PortfolioExecutor
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from abupy.AlphaBu.ABuSelectionStrategies import SelectionPanel
from abupy.AlphaBu.ABuSelectionStrategiesV2 import (
    B1ConstraintEngine, LegacyStrategyIntentAdapter, run_legacy_v2_backtest,
)
from abupy.AlphaBu.ABuTradeIntent import TradeIntent


def make_panel():
    dates = pd.bdate_range("2023-01-02", periods=520).strftime(
        "%Y%m%d").astype(int).to_numpy()
    t = np.arange(len(dates), dtype=float)
    # A broad uptrend with one five-day pullback late in 2024.
    first = 10 + 0.02*t
    first[500:506] -= np.arange(6) * 0.4
    second = 20 + 0.01*t
    close = np.column_stack([first, second]).astype(np.float32)
    opening = close.copy(); high = close*1.01; low = close*0.99
    volume = np.full_like(close, 1_000_000)
    benchmark = 100 + 0.05*t
    base = SelectionPanel(
        dates, ("sz000001", "sz000002"), opening, high, low, close,
        volume, benchmark,
        execution={"open": opening, "high": high, "low": low,
                   "close": close, "volume": volume},
        amount=close*volume,
        market_cap=np.full_like(close, 1e9, dtype=np.float64),
        industry=np.zeros_like(close, dtype=np.int16),
    )
    master = pd.DataFrame({
        "symbol": base.symbols, "list_date": ["2000-01-01"]*2,
        "delist_date": [None]*2, "status": ["listed"]*2,
    })
    return SelectionPanelV2(
        base, master, turnover=np.full_like(close, 0.5),
        st_status_known=np.ones_like(close, dtype=bool),
    )


class SelectionStrategiesV2Test(unittest.TestCase):

    def test_b1_rejects_any_fabricated_stop_or_r(self):
        panel = make_panel(); day = 300
        raw = float(panel.exec_close[day, 0])
        intent = TradeIntent(
            intent_id="bad", strategy_id="legacy", strategy_version="1",
            signal_asof=int(panel.dates[day]), symbol="sz000001",
            signal_price_raw=raw, initial_stop_raw=raw-1,
            r_definition_version="fake",
            metadata={"max_buy_price_raw": raw+0.2},
        )
        executor = PortfolioExecutor(panel, ExecutionConfig(initial_cash=100_000))
        with self.assertRaises(ValueError):
            B1ConstraintEngine(panel).evaluate(executor, intent, day, 100)

    def test_b1_has_no_stop_and_b2_has_mapped_executable_stop(self):
        panel = make_panel(); day = 300
        b1 = LegacyStrategyIntentAdapter(panel, "trend_reversal", "b1")._intent(day, 0)
        b2 = LegacyStrategyIntentAdapter(panel, "trend_reversal", "b2")._intent(day, 0)
        self.assertIsNone(b1.initial_stop_raw)
        self.assertEqual(b1.r_definition_version, "none")
        self.assertLess(b2.initial_stop_raw, b2.signal_price_raw)
        self.assertEqual(b2.strategy_id, "trend_reversal_rstop_v2")
        self.assertAlmostEqual(
            b2.initial_stop_raw,
            b2.initial_stop_adjusted * b2.adjustment_factor_signal,
        )

    def test_future_prices_do_not_change_current_intent(self):
        first = make_panel(); second = make_panel(); day = 300
        second.base.close[day+1:, 0] *= 3
        a = LegacyStrategyIntentAdapter(first, "trend_breakout", "b2")._intent(day, 0)
        b = LegacyStrategyIntentAdapter(second, "trend_breakout", "b2")._intent(day, 0)
        self.assertEqual(a, b)

    def test_b1_and_b2_results_keep_distinct_names(self):
        panel = make_panel()
        b1, _, _, _ = run_legacy_v2_backtest(
            panel, "trend_reversal", 2024, mode="b1", slippage_bps=0)
        b2, _, _, _ = run_legacy_v2_backtest(
            panel, "trend_reversal", 2024, mode="b2", slippage_bps=0)
        self.assertEqual(b1["strategy"], "trend_reversal_v1")
        self.assertEqual(b2["strategy"], "trend_reversal_rstop_v2")
        self.assertEqual(b1["experiment"], "b1")
        self.assertEqual(b2["experiment"], "b2")


if __name__ == "__main__":
    unittest.main()
