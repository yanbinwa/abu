import unittest
from types import SimpleNamespace

import numpy as np

from abupy.AlphaBu.ABuAlpha158Lite import Alpha158LiteConfig
from abupy.AlphaBu.ABuAlphaExitPolicy import (
    Alpha158ExitOverlayEngine, AlphaExitOverlayConfig,
)


class AlphaExitOverlayTest(unittest.TestCase):

    @staticmethod
    def panel(closes, atr=.2):
        values = np.asarray(closes, dtype=float)[:, None]
        return SimpleNamespace(
            close=values, exec_close=values, atr21=np.full_like(values, atr),
            ma10=values.copy(), symbol_index={"sz000001": 0})

    @staticmethod
    def register(engine, entry=10., stop=9.):
        intent = SimpleNamespace(
            symbol="sz000001", adjustment_factor_signal=1.,
            initial_stop_adjusted=stop)
        fill = SimpleNamespace(fill_price_raw=entry)
        engine.register_entry(intent, fill, 0)

    def test_breakeven_floor_triggers_before_loose_atr_stop(self):
        panel = self.panel([10., 11.1, 10.04], atr=.5)
        engine = Alpha158ExitOverlayEngine(
            panel, Alpha158LiteConfig(), AlphaExitOverlayConfig(
                breakeven_floor_enabled=True,
                breakeven_cost_buffer_bps=50.))
        self.register(engine)
        self.assertIsNone(engine.signal(1, "sz000001"))
        self.assertAlmostEqual(
            engine.states["sz000001"].current_stop_adjusted, 10.05)
        self.assertEqual(engine.signal(2, "sz000001"), "TRAILING_STOP")

    def test_rolling_stagnation_requires_age_peak_age_and_ma20_break(self):
        closes = [10.] + [10.6] + [10.55] * 30
        panel = self.panel(closes, atr=.4)
        engine = Alpha158ExitOverlayEngine(
            panel, Alpha158LiteConfig(), AlphaExitOverlayConfig(
                rolling_stagnation_enabled=True,
                rolling_min_holding_sessions=20,
                rolling_no_new_peak_sessions=10))
        self.register(engine)
        for day in range(1, 20):
            self.assertIsNone(engine.signal(day, "sz000001"))
        self.assertEqual(
            engine.signal(20, "sz000001"), "ROLLING_STAGNATION")

    def test_rolling_stagnation_does_not_replace_active_trailing_stop(self):
        closes = [10., 11.1] + [10.8] * 20
        panel = self.panel(closes, atr=1.)
        engine = Alpha158ExitOverlayEngine(
            panel, Alpha158LiteConfig(), AlphaExitOverlayConfig(
                rolling_stagnation_enabled=True,
                rolling_min_holding_sessions=5,
                rolling_no_new_peak_sessions=3,
                rolling_require_below_ma20=False))
        self.register(engine)
        for day in range(1, len(closes)):
            self.assertIsNone(engine.signal(day, "sz000001"))
        self.assertTrue(engine.states["sz000001"].trailing_enabled)


if __name__ == "__main__":
    unittest.main()
