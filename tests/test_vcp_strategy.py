"""Exact-window VCP signal and event-exit tests."""

import unittest
from types import SimpleNamespace

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from abupy.AlphaBu.ABuSelectionStrategies import SelectionPanel
from abupy.AlphaBu.ABuTradeIntent import TradeIntent
from abupy.AlphaBu.ABuVCPStrategy import (
    VCPExitEngine, VCPStrategy, make_vcp_exit_engine, run_vcp_backtest,
)


def make_vcp_panel():
    dates = pd.bdate_range("2023-01-02", periods=300).strftime(
        "%Y%m%d").astype(int).to_numpy()
    n = len(dates); shape = (n, 2)
    close = np.full(shape, 10.0, dtype=np.float32)
    opening = close.copy(); high = close*1.01; low = close*0.99
    volume = np.full(shape, 1_000_000, dtype=np.float32)
    day = 260
    # Control is wide; contraction is tight. Signal day is excluded from both.
    high[day-80:day-20] = 12.0; low[day-80:day-20] = 8.0
    high[day-20:day] = 10.0; low[day-20:day] = 9.6
    close[day] = 10.5; opening[day] = 10.5
    high[day] = 10.6; low[day] = 10.4
    amount = np.full(shape, 100.0, dtype=np.float32)
    turnover = np.full(shape, 1.0, dtype=np.float32)
    amount[day-5:day] = 60; amount[day] = 200
    turnover[day-5:day] = 0.7; turnover[day] = 1.5
    benchmark = np.linspace(100, 120, n)
    base = SelectionPanel(
        dates, ("sz000001", "sz000002"), opening, high, low, close,
        volume, benchmark,
        execution={"open": opening, "high": high, "low": low,
                   "close": close, "volume": volume},
        amount=amount, market_cap=np.full(shape, 1e9),
        industry=np.zeros(shape, dtype=np.int16),
    )
    # Freeze indicator inputs so the test isolates exact VCP formulas.
    base.ma120[:] = 9.0
    for row in range(day-19, day+1):
        base.ma120[row] = 8.8 + (row-(day-19))*0.01
    base.ma60[:] = 9.5
    base.market_ma200[:] = 90
    base.atr21[:] = 0.5
    base.atr21[day-1] = 0.1
    base.atr21[day] = 0.2
    master = pd.DataFrame({
        "symbol": base.symbols, "list_date": ["2000-01-01"]*2,
        "delist_date": [None]*2, "status": ["listed"]*2,
    })
    panel = SelectionPanelV2(
        base, master, turnover=turnover,
        st_status_known=np.ones(shape, dtype=bool),
    )
    return panel, day


class VCPStrategyTest(unittest.TestCase):

    def test_core_uses_prebreakout_windows_and_is_future_invariant(self):
        panel, day = make_vcp_panel()
        strategy = VCPStrategy(panel)
        first = strategy.generate_intents(day, "core")
        self.assertEqual(len(first), 2)
        self.assertTrue(all(item.initial_stop_adjusted < item.signal_price_adjusted
                            for item in first))
        # Neither the breakout day's extreme nor any future bar may alter the
        # contraction/control windows or today's intent.
        panel.high[day] = 1000
        panel.close[day+1:] *= 3
        second = VCPStrategy(panel).generate_intents(day, "core")
        self.assertEqual(first, second)

    def test_attention_rules_use_common_coverage_and_prebreakout_dryness(self):
        panel, day = make_vcp_panel()
        # Keep amplitude dry/expansion conditions explicit.
        previous = panel.close[day-1].copy()
        panel.high[day-20:day] = 10.05
        panel.low[day-20:day] = 9.95
        panel.high[day-5:day] = 10.02
        panel.low[day-5:day] = 9.98
        panel.high[day] = previous * 1.02
        panel.low[day] = previous * 0.98
        intents = VCPStrategy(panel).generate_intents(day, "attention_common")
        self.assertEqual(len(intents), 2)
        self.assertTrue(all(item.strategy_id == "vcp_attention_v1" for item in intents))
        panel.turnover[day-1, 0] = np.nan
        missing = VCPStrategy(panel).generate_intents(day, "attention_common")
        self.assertEqual([item.symbol for item in missing], ["sz000002"])

    def test_planned_risk_above_eight_percent_is_rejected(self):
        panel, day = make_vcp_panel()
        panel.atr21[day] = 2.0
        # Structure low 9.6 and max price 12.1 imply risk > 8% of 10.5.
        self.assertEqual(VCPStrategy(panel).generate_intents(day, "core"), [])

    def test_residual_variant_requires_positive_past_residual(self):
        panel, day = make_vcp_panel()
        start, end = day - 251, day - 20
        panel.returns[start:end] = 0.0
        panel.returns[day-125:end, 0] = 0.01
        panel.returns[day-125:end, 1] = -0.01
        intents = VCPStrategy(panel).generate_intents(day, "residual_core")
        self.assertEqual([item.symbol for item in intents], ["sz000001"])
        self.assertEqual(intents[0].strategy_id, "vcp_residual_v2")
        self.assertGreater(intents[0].metadata["residual_momentum"], 0)

    def test_exit_priority_and_trailing_stop_only_move_up(self):
        panel, day = make_vcp_panel()
        intent = TradeIntent(
            intent_id="vcp", strategy_id="vcp_core_v1", strategy_version="1",
            signal_asof=int(panel.dates[day]), symbol="sz000001",
            signal_price_adjusted=10.5, signal_price_raw=10.5,
            adjustment_factor_signal=1.0, initial_stop_adjusted=10.0,
            initial_stop_raw=10.0,
            metadata={"breakout_level": 10.2},
        )
        engine = VCPExitEngine(panel)
        engine.register_entry(intent, SimpleNamespace(fill_price_raw=10.5), day)
        panel.close[day+1, 0] = 9.9
        panel.benchmark_close[day+1] = 0
        self.assertEqual(engine.signal(day+1, "sz000001"), "INITIAL_STOP")

        panel.close[day+1, 0] = 11.2
        panel.benchmark_close[day+1] = 120
        self.assertIsNone(engine.signal(day+1, "sz000001"))
        raised = engine.states["sz000001"].current_stop_adjusted
        panel.close[day+2, 0] = 11.0
        engine.signal(day+2, "sz000001")
        self.assertGreaterEqual(engine.states["sz000001"].current_stop_adjusted,
                                raised)

    def test_exit_profiles_execute_initial_stop_and_isolate_rules(self):
        panel, day = make_vcp_panel()
        intent = TradeIntent(
            intent_id="vcp", strategy_id="vcp_core_v1", strategy_version="1",
            signal_asof=int(panel.dates[day]), symbol="sz000001",
            signal_price_adjusted=10.5, signal_price_raw=10.5,
            adjustment_factor_signal=1.0, initial_stop_adjusted=10.0,
            initial_stop_raw=10.0, metadata={"breakout_level": 10.2},
        )
        stop = make_vcp_exit_engine(panel, "stop_fixed20_v2")
        stop.register_entry(intent, SimpleNamespace(fill_price_raw=10.5), day)
        panel.close[day+1, 0] = 9.9
        self.assertEqual(stop.signal(day+1, "sz000001"), "INITIAL_STOP")

        trailing = make_vcp_exit_engine(panel, "stop_trailing_fixed20_v2")
        trailing.register_entry(intent, SimpleNamespace(fill_price_raw=10.5), day)
        panel.close[day+1, 0] = 10.1
        self.assertIsNone(trailing.signal(day+1, "sz000001"))

    def test_c_and_e_experiments_use_distinct_exit_and_sizing_paths(self):
        panel, _ = make_vcp_panel()
        fixed, _, _, _, fixed_exits = run_vcp_backtest(
            panel, 2024, "c_core_fixed20", slippage_bps=0)
        event, _, _, _, event_exits = run_vcp_backtest(
            panel, 2024, "e_core_r_event", slippage_bps=0)
        self.assertEqual(fixed["experiment"], "c_core_fixed20")
        self.assertEqual(event["experiment"], "e_core_r_event")
        self.assertGreaterEqual(fixed["filled_buys"], 1)
        self.assertGreaterEqual(event["risk_decisions"], 1)
        self.assertTrue(all(item["reason"] == "FIXED_HOLD" for item in fixed_exits))
        self.assertTrue(all(item["reason"] != "FIXED_HOLD" for item in event_exits))


if __name__ == "__main__":
    unittest.main()
