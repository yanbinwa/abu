from types import SimpleNamespace
import unittest

import numpy as np
import pandas as pd

from scripts.analyze_vcp_baseline_diagnostics import (
    approval_diagnostics, benchmark_ma200_backtest,
    candidate_forward_outcomes,
)


class VCPBaselineDiagnosticsTest(unittest.TestCase):

    def test_ma200_baseline_uses_prior_close_and_next_open(self):
        dates = np.arange(20200101, 20200101 + 205)
        frame = pd.DataFrame({
            "date": dates,
            "open": np.r_[np.ones(200) * 10, [10, 12, 12, 12, 12]],
            "close": np.r_[np.ones(200) * 10, [11, 12, 12, 12, 12]],
        })
        summary, curve, annual = benchmark_ma200_backtest(
            frame, int(dates[200]), int(dates[-1]), slippage_bps=0)
        self.assertEqual(curve.iloc[0].exposure, 0)
        self.assertEqual(curve.iloc[1].exposure, 1)
        self.assertEqual(summary["return_pct"], 0)
        self.assertEqual(summary["average_exposure_pct"], 80)
        self.assertEqual(len(annual), 1)

    def test_candidate_forward_outcomes_and_cluster_uplift(self):
        dates = np.arange(20200101, 20200101 + 50)
        close = np.c_[np.linspace(10, 20, 50), np.linspace(10, 5, 50)]
        panel = SimpleNamespace(
            dates=dates,
            symbols=["up", "down"],
            symbol_index={"up": 0, "down": 1},
            close=close,
            high=close * 1.01,
            low=close * .99,
        )
        intents = pd.DataFrame([
            {"intent_id": "a", "signal_asof": int(dates[5]), "symbol": "up",
             "score": 1.0, "signal_price_adjusted": close[5, 0],
             "initial_stop_adjusted": close[5, 0] - 1},
            {"intent_id": "b", "signal_asof": int(dates[5]), "symbol": "down",
             "score": 0.0, "signal_price_adjusted": close[5, 1],
             "initial_stop_adjusted": close[5, 1] - 1},
        ])
        decisions = pd.DataFrame([
            {"intent_id": "a", "signal_asof": int(dates[5]), "symbol": "up",
             "decision": "approved", "reason_codes": []},
            {"intent_id": "b", "signal_asof": int(dates[5]), "symbol": "down",
             "decision": "rejected", "reason_codes": ["PORTFOLIO_OPEN_RISK"]},
        ])
        outcomes = candidate_forward_outcomes(panel, intents, decisions)
        indexed = outcomes.set_index("intent_id")
        self.assertGreater(indexed.loc["a", "return_20d"], 0)
        self.assertLess(indexed.loc["b", "return_20d"], 0)
        grouped, uplift, annual = approval_diagnostics(
            outcomes, paths=200, seed=7)
        return_uplift = uplift.set_index("metric").loc["return_20d"]
        self.assertGreater(return_uplift["mean"], 0)
        self.assertEqual(return_uplift["clusters"], 1)
        self.assertEqual(set(grouped.approval_group), {"allowed", "rejected"})
        self.assertEqual(set(annual.approval_group), {"allowed", "rejected"})


if __name__ == "__main__":
    unittest.main()
