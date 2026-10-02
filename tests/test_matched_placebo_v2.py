"""Causality and executable-path tests for matched placebo v2."""

import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuMatchedPlaceboV2 import (
    MatchedPlaceboV2, PlaceboConfig, summarize_placebos,
)
from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from abupy.AlphaBu.ABuSelectionStrategies import SelectionPanel
from abupy.AlphaBu.ABuTradeIntent import TradeIntent


def make_panel(future_candidate_suspended=False):
    dates = pd.bdate_range("2024-01-02", periods=55).strftime("%Y%m%d").astype(int).to_numpy()
    t = np.arange(len(dates))
    close = np.column_stack((10 + 0.03*t, 20 + 0.02*t, 30 + 0.01*t)).astype(np.float32)
    opening = close.copy(); high = close*1.01; low = close*0.99
    volume = np.full_like(close, 100_000)
    if future_candidate_suspended:
        opening[41, 1] = high[41, 1] = low[41, 1] = close[41, 1] = np.nan
        volume[41, 1] = 0
    amount = close * volume
    cap = np.full_like(close, 1_000_000_000, dtype=np.float64)
    industry = np.zeros_like(close, dtype=np.int16)
    base = SelectionPanel(
        dates, ("sz000001", "sz000002", "sz000003"), opening, high, low,
        close, volume, 100 + 0.02*t,
        execution={"open": opening, "high": high, "low": low,
                   "close": close, "volume": volume},
        amount=amount, market_cap=cap, industry=industry,
    )
    master = pd.DataFrame({
        "symbol": base.symbols, "list_date": ["2000-01-01"]*3,
        "delist_date": [None]*3, "status": ["listed"]*3,
    })
    return SelectionPanelV2(
        base, master, turnover=np.full_like(close, 0.5),
        st_status_known=np.ones_like(close, dtype=bool),
    )


def signal_intent(panel, target="sz000001"):
    day = 40; column = panel.symbol_index[target]
    raw = float(panel.exec_close[day, column]); adj = float(panel.close[day, column])
    return TradeIntent(
        intent_id="actual", strategy_id="test", strategy_version="1",
        signal_asof=int(panel.dates[day]), symbol=target, score=1.0,
        signal_price_adjusted=adj, signal_price_raw=raw,
        adjustment_factor_signal=raw/adj,
        initial_stop_adjusted=adj*0.92, initial_stop_raw=raw*0.92,
        metadata={"stop_fraction": 0.08, "hold_sessions": 5,
                  "target_notional": 10_000, "max_gap_fraction": 0.03},
    )


class MatchedPlaceboV2Test(unittest.TestCase):

    def test_future_data_does_not_change_matching_pool(self):
        normal = make_panel(False); altered = make_panel(True)
        config = PlaceboConfig(min_history=20, pool_size=10)
        first = MatchedPlaceboV2(normal, config).matching_pool(signal_intent(normal))
        second = MatchedPlaceboV2(altered, config).matching_pool(signal_intent(altered))
        self.assertEqual(first.tolist(), second.tolist())

    def test_future_suspension_is_not_resampled(self):
        panel = make_panel(True)
        runner = MatchedPlaceboV2(
            panel, PlaceboConfig(min_history=20, pool_size=1, seed=3)
        )
        result, _, fills, diagnostics = runner.run_once(
            [signal_intent(panel)], seed=3,
            execution_config=ExecutionConfig(initial_cash=100_000, slippage_bps=0),
        )
        self.assertEqual(result["matched_intents"], 1)
        self.assertEqual(diagnostics.iloc[0].chosen_symbol, "sz000002")
        buy = fills[fills.side == "buy"].iloc[0]
        self.assertEqual(buy.status, "rejected")
        self.assertEqual(buy.reason_code, "NOT_BUY_TRADABLE")

    def test_distribution_is_reproducible_and_uses_own_costs(self):
        panel = make_panel(False)
        config = PlaceboConfig(min_history=20, pool_size=2, seed=7)
        runner = MatchedPlaceboV2(panel, config)
        first = runner.run_distribution([signal_intent(panel)], replicates=4)
        second = runner.run_distribution([signal_intent(panel)], replicates=4)
        pd.testing.assert_frame_equal(first, second)
        self.assertTrue((first.filled_buys == 1).all())
        self.assertTrue(np.isfinite(first.return_pct).all())

    def test_checkpoint_chunks_merge_to_same_distribution(self):
        panel = make_panel(False)
        runner = MatchedPlaceboV2(
            panel, PlaceboConfig(min_history=20, pool_size=2, seed=31)
        )
        complete = runner.run_distribution([signal_intent(panel)], replicates=4)
        first = runner.run_distribution(
            [signal_intent(panel)], replicate_ids=[0, 2]
        )
        second = runner.run_distribution(
            [signal_intent(panel)], replicate_ids=[1, 3]
        )
        resumed = pd.concat([first, second]).sort_values(
            "replicate").reset_index(drop=True)
        pd.testing.assert_frame_equal(complete, resumed)
        summary = summarize_placebos(complete, actual_return_pct=0.0)
        self.assertEqual(summary["replicates"], 4)
        self.assertIn("actual_percentile", summary)

    def test_exit_policy_is_applied_to_substitute_position(self):
        panel = make_panel(False)

        class ImmediateExit:
            def __init__(self, _panel):
                self.entries = {}
            def register_entry(self, intent, fill, day):
                self.entries[intent.symbol] = day
            def signal(self, day, symbol, fixed_hold=False):
                return "EVENT" if day > self.entries[symbol] else None
            def remove(self, symbol):
                self.entries.pop(symbol, None)

        source = signal_intent(panel)
        source = TradeIntent(**{**source.__dict__,
                                "metadata": {**source.metadata,
                                             "hold_sessions": 20}})
        result, _, fills, _ = MatchedPlaceboV2(
            panel, PlaceboConfig(min_history=20, pool_size=1, seed=4)
        ).run_once([source], seed=4,
                   execution_config=ExecutionConfig(initial_cash=100_000,
                                                    slippage_bps=0),
                   exit_policy_factory=ImmediateExit)
        sold = fills[(fills.side == "sell") & (fills.status == "filled")]
        self.assertEqual(len(sold), 1)
        self.assertEqual(result["open_positions"], 0)


if __name__ == "__main__":
    unittest.main()
